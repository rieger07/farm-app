import ast
from pathlib import Path
import unittest

from fastapi.testclient import TestClient

from app import app as homestead_app
from inventory.app import app as inventory_app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _imported_roots(source: str, filename: str) -> list[tuple[int, str]]:
    tree = ast.parse(source, filename=filename)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module))
    return out


def _matches_prefix(mod_name: str, prefixes: tuple[str, ...]) -> bool:
    return any(mod_name == p or mod_name.startswith(p + ".") for p in prefixes)


def _runtime_python_files() -> list[Path]:
    files = [REPO_ROOT / "app.py"]
    for pkg in ("homestead", "inventory", "shared"):
        files.extend(sorted((REPO_ROOT / pkg).rglob("*.py")))
    return files


class ArchitectureGuardrailsTest(unittest.TestCase):
    def test_no_cross_app_imports(self):
        """Enforce clean dependency direction: apps may import shared/*, never each other."""
        violations: list[str] = []
        for py_path in _runtime_python_files():
            rel = py_path.relative_to(REPO_ROOT).as_posix()
            if rel == "app.py" or rel.startswith("homestead/"):
                forbidden = ("inventory",)
            elif rel.startswith("inventory/"):
                forbidden = ("homestead", "app")
            elif rel == "shared/db/migrations/0001_baseline.py":
                # Shipped baseline migration is append-only/immutable and already imports homestead.config.
                forbidden = ("inventory", "app")
            else:
                forbidden = ("homestead", "inventory", "app")

            source = py_path.read_text(encoding="utf-8")
            for lineno, mod_name in _imported_roots(source, rel):
                if _matches_prefix(mod_name, forbidden):
                    violations.append(f"{rel}:{lineno}: imports {mod_name}")

        self.assertEqual(
            violations,
            [],
            "Cross-app imports are forbidden; move shared logic into shared/:\n"
            + "\n".join(violations),
        )

    def test_no_legacy_streamlit_code(self):
        """Ensure unused Streamlit UI/telemetry code stays removed."""
        self.assertFalse((REPO_ROOT / "homestead/ui").exists())
        self.assertFalse((REPO_ROOT / "homestead/telemetry.py").exists())
        violations: list[str] = []
        for py_path in _runtime_python_files():
            rel = py_path.relative_to(REPO_ROOT).as_posix()
            source = py_path.read_text(encoding="utf-8")
            for lineno, mod_name in _imported_roots(source, rel):
                if _matches_prefix(mod_name, ("streamlit", "streamlit_timeline")):
                    violations.append(f"{rel}:{lineno}: imports {mod_name}")
        self.assertEqual(violations, [])

    def test_shared_static_qr_assets_and_sw(self):
        """Ensure qrcode.min.js and pay_qr.js are served from /shared-static and cached by sw.js."""
        self.assertFalse((REPO_ROOT / "homestead/static/qrcode.min.js").exists())
        self.assertFalse((REPO_ROOT / "inventory/static/qrcode.min.js").exists())
        for label, fastapi_app in (("homestead", homestead_app), ("inventory", inventory_app)):
            client = TestClient(fastapi_app)
            for asset_path in ("/shared-static/qrcode.min.js", "/shared-static/pay_qr.js"):
                resp = client.get(asset_path)
                self.assertEqual(resp.status_code, 200, f"{label} {asset_path}")
                self.assertEqual(resp.headers.get("cache-control"), "no-cache", f"{label} {asset_path}")

        sw = (REPO_ROOT / "inventory/static/sw.js").read_text(encoding="utf-8")
        self.assertIn("'/shared-static/qrcode.min.js'", sw)
        self.assertIn("'/shared-static/pay_qr.js'", sw)

    def test_dockerfile_requirements_and_ci_gates(self):
        """Ensure Dockerfiles share requirements.txt and GitHub Actions gates releases on tests."""
        self.assertTrue((REPO_ROOT / "requirements.txt").is_file())
        self.assertFalse((REPO_ROOT / "requirements-inventory.txt").exists())

        df = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        df_inv = (REPO_ROOT / "Dockerfile.inventory").read_text(encoding="utf-8")
        for label, content in (("Dockerfile", df), ("Dockerfile.inventory", df_inv)):
            self.assertIn("-r requirements.txt", content, label)
            self.assertNotIn("requirements-inventory.txt", content, label)

        self.assertIn("COPY --chown=appuser:appgroup shared/ ./shared/", df_inv)
        self.assertIn("COPY --chown=appuser:appgroup homestead/ ./homestead/", df_inv)
        self.assertIn("COPY --chown=appuser:appgroup inventory/ ./inventory/", df_inv)

        ci_yml = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("unittest discover -s tests -t .", ci_yml)

        rel_yml = (REPO_ROOT / ".github/workflows/docker-image.yml").read_text(encoding="utf-8")
        self.assertIn("unittest discover -s tests -t .", rel_yml)
        self.assertIn("needs: test", rel_yml)


if __name__ == "__main__":
    unittest.main()
