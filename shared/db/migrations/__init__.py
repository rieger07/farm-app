"""Zero-dependency ordered SQLite migration runner for the shared database."""

from __future__ import annotations

from datetime import datetime
import importlib
from pathlib import Path
import re
import sqlite3
from types import ModuleType

from shared.db.connection import get_connection

_MIGRATION_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.py$")
_MIGRATIONS_DIR = Path(__file__).parent

__all__ = ["list_migrations", "migrate"]


def list_migrations() -> list[tuple[int, str, ModuleType]]:
    """Discover and validate sequential migration modules in ``shared/db/migrations/``."""
    discovered: list[tuple[int, str, str]] = []
    for path in sorted(_MIGRATIONS_DIR.iterdir()):
        if not path.is_file():
            continue
        m = _MIGRATION_FILE_RE.match(path.name)
        if not m:
            continue
        discovered.append((int(m.group(1)), m.group(2), path.stem))

    discovered.sort(key=lambda item: item[0])
    loaded: list[tuple[int, str, ModuleType]] = []
    expected_version = 1
    seen_versions: set[int] = set()

    for version, _name_slug, stem in discovered:
        if version in seen_versions:
            raise RuntimeError(f"Duplicate migration version {version:04d} ({stem}).")
        if version != expected_version:
            raise RuntimeError(
                f"Expected migration version {expected_version:04d}, found {version:04d} ({stem})."
            )
        seen_versions.add(version)
        expected_version += 1

        mod = importlib.import_module(f"shared.db.migrations.{stem}")
        mod_version = getattr(mod, "VERSION", None)
        mod_name = getattr(mod, "NAME", None)
        upgrade_fn = getattr(mod, "upgrade", None)
        if mod_version != version:
            raise RuntimeError(
                f"Migration {stem} defines VERSION={mod_version!r}, expected {version}."
            )
        if not isinstance(mod_name, str) or not mod_name.strip():
            raise RuntimeError(f"Migration {stem} must define a non-empty NAME string.")
        if not callable(upgrade_fn):
            raise RuntimeError(f"Migration {stem} must define a callable upgrade(conn).")
        loaded.append((version, mod_name.strip(), mod))

    return loaded


def migrate() -> list[int]:
    """Enable WAL mode and apply any unapplied numbered migrations under a write lock."""
    migrations = list_migrations()
    conn: sqlite3.Connection = get_connection(isolation_level=None)
    applied_now: list[int] = []
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied_versions = {
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, name, mod in migrations:
            if version in applied_versions:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                already = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if already:
                    conn.execute("ROLLBACK")
                    continue
                mod.upgrade(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.utcnow().isoformat(timespec="seconds")),
                )
                conn.execute("COMMIT")
                applied_now.append(version)
                applied_versions.add(version)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    return applied_now
