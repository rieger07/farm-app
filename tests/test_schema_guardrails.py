import ast
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from shared.db import get_connection, list_migrations, migrate

REPO_ROOT = Path(__file__).resolve().parent.parent
DDL_PATTERN = re.compile(r"\b(?:CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)|ALTER\s+TABLE)\b", re.IGNORECASE)


def _iter_non_docstring_ast_strings(source: str, filename: str) -> list[tuple[int, str]]:
    """Return (lineno, value) for all string constants in Python source except module/class/def docstrings."""
    tree = ast.parse(source, filename=filename)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_nodes.add(id(node.body[0].value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in docstring_nodes:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((getattr(node, "lineno", 0), node.value))
        elif isinstance(node, ast.JoinedStr):
            joined = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if joined:
                out.append((getattr(node, "lineno", 0), joined))
    return out


class SchemaGuardrailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_db = os.environ.get("FARM_DB_PATH")
        os.environ["FARM_DB_PATH"] = os.path.join(self.tmp.name, "guardrail.db")

    def tearDown(self):
        if self.prev_db is None:
            os.environ.pop("FARM_DB_PATH", None)
        else:
            os.environ["FARM_DB_PATH"] = self.prev_db
        self.tmp.cleanup()

    def test_no_ddl_outside_shared_migrations(self):
        """Forbid CREATE TABLE/INDEX and ALTER TABLE in runtime app modules outside shared/db/migrations/."""
        runtime_files: list[Path] = [REPO_ROOT / "app.py"]
        for pkg in ("homestead", "inventory", "shared"):
            pkg_dir = REPO_ROOT / pkg
            for py_path in sorted(pkg_dir.rglob("*.py")):
                rel = py_path.relative_to(REPO_ROOT).as_posix()
                if rel.startswith("shared/db/migrations/"):
                    continue
                runtime_files.append(py_path)

        violations: list[str] = []
        for py_path in runtime_files:
            rel = py_path.relative_to(REPO_ROOT).as_posix()
            source = py_path.read_text(encoding="utf-8")
            for lineno, text in _iter_non_docstring_ast_strings(source, rel):
                if DDL_PATTERN.search(text):
                    violations.append(f"{rel}:{lineno}: {text.strip()[:80]!r}")

        self.assertEqual(
            violations,
            [],
            "DDL statements must live exclusively in shared/db/migrations/:\n" + "\n".join(violations),
        )

    def test_migration_numbering_and_contract(self):
        migrations = list_migrations()
        self.assertGreaterEqual(len(migrations), 1)
        versions = [v for v, _, _ in migrations]
        self.assertEqual(versions, list(range(1, len(migrations) + 1)))
        for version, name, mod in migrations:
            self.assertEqual(mod.VERSION, version)
            self.assertTrue(name)
            self.assertTrue(callable(mod.upgrade))

    def test_migrate_idempotent_and_wal_enabled(self):
        first = migrate()
        expected = [v for v, _, _ in list_migrations()]
        self.assertEqual(first, expected)

        second = migrate()
        self.assertEqual(second, [])

        conn = get_connection()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            applied = [
                int(r["version"])
                for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            ]
            self.assertEqual(applied, expected)
        finally:
            conn.close()

    def test_migrate_upgrades_legacy_db_preserving_live_picking_slug(self):
        """Simulate an older pre-migration DB and verify safe column upgrades & slug preservation."""
        db_path = os.environ["FARM_DB_PATH"]
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE merchants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE marketplaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    booth_fee_cents INTEGER NOT NULL DEFAULT 0,
                    travel_cost_cents INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE market_days (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant_id INTEGER NOT NULL,
                    market_date TEXT NOT NULL,
                    marketplace_id INTEGER NOT NULL,
                    booth_fee_cents INTEGER NOT NULL DEFAULT 0,
                    travel_cost_cents INTEGER NOT NULL DEFAULT 0,
                    weather TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    name TEXT NOT NULL,
                    species TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    days_to_birth INTEGER NOT NULL,
                    weeks_to_proc INTEGER NOT NULL,
                    feed_total REAL NOT NULL,
                    picking_slug TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE egg_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    total_collected INTEGER NOT NULL DEFAULT 0,
                    cracked_unusable INTEGER NOT NULL DEFAULT 0,
                    home_used INTEGER NOT NULL DEFAULT 0,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                INSERT INTO batches (
                    id, user_id, name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total, picking_slug, created_at
                ) VALUES (
                    1, 1, 'Thanksgiving 2026', 'turkey chicks', 25, '2026-06-02', '2026-06-02',
                    0, 18, 75.0, 'live-turkey-2026', '2026-06-02T00:00:00'
                );
                INSERT INTO batches (
                    id, user_id, name, species, quantity, start_date, end_date,
                    days_to_birth, weeks_to_proc, feed_total, picking_slug, created_at
                ) VALUES (
                    2, 1, 'Summer Broilers', 'cornish cross', 30, '2026-06-01', '2026-06-01',
                    0, 8, 15.0, '', '2026-06-01T00:00:00'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        applied = migrate()
        self.assertIn(1, applied)

        c2 = get_connection()
        try:
            m_cols = {r["name"] for r in c2.execute("PRAGMA table_info(merchants)").fetchall()}
            self.assertIn("venmo_handle", m_cols)
            self.assertIn("paypal_handle", m_cols)

            md_cols = {r["name"] for r in c2.execute("PRAGMA table_info(market_days)").fetchall()}
            self.assertIn("status", md_cols)
            self.assertIn("starting_cash_cents", md_cols)

            eg_cols = {r["name"] for r in c2.execute("PRAGMA table_info(egg_logs)").fetchall()}
            self.assertIn("species", eg_cols)
            self.assertIn("created_by", eg_cols)

            b1 = c2.execute("SELECT picking_slug, starter_weeks, lifecycle_type FROM batches WHERE id = 1").fetchone()
            self.assertEqual(b1["picking_slug"], "live-turkey-2026")
            self.assertIsNotNone(b1["starter_weeks"])
            self.assertIsNotNone(b1["lifecycle_type"])

            b2 = c2.execute("SELECT picking_slug FROM batches WHERE id = 2").fetchone()
            self.assertTrue(b2["picking_slug"])
        finally:
            c2.close()

    def test_dockerfile_inventory_copies_shared_and_rules_exist(self):
        df_inv = (REPO_ROOT / "Dockerfile.inventory").read_text(encoding="utf-8")
        self.assertIn("COPY --chown=appuser:appgroup shared/ ./shared/", df_inv)
        self.assertTrue((REPO_ROOT / "AGENTS.md").is_file())
        self.assertTrue((REPO_ROOT / "CLAUDE.md").is_file())


if __name__ == "__main__":
    unittest.main()
