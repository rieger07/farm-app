from pathlib import Path
import re
import unittest

from fastapi.testclient import TestClient

from app import app as homestead_app
from inventory.app import app as inventory_app

REPO_ROOT = Path(__file__).resolve().parent.parent


class ThemeGuardrailsTest(unittest.TestCase):
    def test_both_apps_serve_shared_theme_css(self):
        for label, fastapi_app in (("homestead", homestead_app), ("inventory", inventory_app)):
            client = TestClient(fastapi_app)
            resp = client.get("/shared-static/theme.css")
            self.assertEqual(resp.status_code, 200, label)
            self.assertIn("text/css", resp.headers.get("content-type", ""), label)
            self.assertEqual(resp.headers.get("cache-control"), "no-cache", label)
            self.assertIn("--green:#2d5a43", resp.text, label)
            self.assertIn("details.hist-row", resp.text, label)
            self.assertIn(".btn.sm", resp.text, label)

    def test_both_base_templates_load_theme_before_app_style(self):
        for rel in ("homestead/templates/base.html", "inventory/templates/base.html"):
            html = (REPO_ROOT / rel).read_text(encoding="utf-8")
            theme_idx = html.find("shared_asset('theme.css')")
            style_idx = html.find("asset('style.css')")
            self.assertGreater(theme_idx, -1, rel)
            self.assertGreater(style_idx, theme_idx, f"{rel} must load theme.css before style.css")

    def test_app_stylesheets_do_not_duplicate_shared_tokens_or_core_primitives(self):
        forbidden = (
            r"--bg\s*:",
            r"--green\s*:",
            r"--sage\s*:",
            r"(?m)^\s*\.shell\s*\{",
            r"(?m)^\s*\.sidebar\s*\{",
            r"(?m)^\s*\.bottomnav\s*\{",
            r"\.btn\.primary\s*\{",
        )
        for rel in ("homestead/static/style.css", "inventory/static/style.css"):
            css = (REPO_ROOT / rel).read_text(encoding="utf-8")
            for pat in forbidden:
                self.assertIsNone(
                    re.search(pat, css),
                    f"{rel} must not duplicate shared theme rule {pat!r} (put it in shared/static/theme.css)",
                )

    def test_service_worker_caches_shared_theme(self):
        sw = (REPO_ROOT / "inventory/static/sw.js").read_text(encoding="utf-8")
        self.assertIn("'/shared-static/theme.css'", sw)
        self.assertIn("/shared-static/", sw)


if __name__ == "__main__":
    unittest.main()
