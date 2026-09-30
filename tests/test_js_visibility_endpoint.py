"""
Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md.

GET /api/js-visibility/pages -- writes real sidecars into a throwaway site
directory under the project root (the same convention
api/workers/audit_worker.py's _safe_dir already uses: a bare sanitized
website name, resolved relative to the process's working directory), then
cleans it up.
"""
import shutil
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from core.js_visibility import write_js_visibility_facts

WEBSITE = "jsvis-test-fixture.example"


def _facts(ratio, missing=None):
    return {
        "measured": True, "text_visibility_ratio": ratio,
        "rendered_word_count": 100, "raw_word_count": int(100 * ratio) if ratio is not None else 0,
        "missing_elements": missing or [], "bot_status_code": 200,
        "bot_blocked_or_errored": False, "bot_fetch_error": None,
    }


class TestJsVisibilityPagesEndpoint(unittest.TestCase):
    def setUp(self):
        self.html_dir = Path(WEBSITE) / "input_html"
        self.html_dir.mkdir(parents=True, exist_ok=True)

        (self.html_dir / "good.html").write_text("<html></html>", encoding="utf-8")
        write_js_visibility_facts(str(self.html_dir / "good.html"), _facts(0.95))

        (self.html_dir / "bad.html").write_text("<html></html>", encoding="utf-8")
        write_js_visibility_facts(str(self.html_dir / "bad.html"),
                                  _facts(0.1, missing=[{"element": "h1", "detail": "x"}]))

        (self.html_dir / "never_measured.html").write_text("<html></html>", encoding="utf-8")
        # No sidecar written for this one on purpose.

        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(Path(WEBSITE), ignore_errors=True)

    def test_only_measured_pages_are_returned(self):
        body = self.client.get("/api/js-visibility/pages", params={"website": WEBSITE}).json()
        self.assertEqual(body["pages_measured"], 2)
        files = {p["file"] for p in body["pages"]}
        self.assertEqual(files, {"good.html", "bad.html"})

    def test_worst_visibility_ratio_comes_first(self):
        body = self.client.get("/api/js-visibility/pages", params={"website": WEBSITE}).json()
        self.assertEqual(body["pages"][0]["file"], "bad.html")
        self.assertEqual(body["pages"][1]["file"], "good.html")

    def test_a_site_never_scraped_returns_an_empty_list_not_an_error(self):
        body = self.client.get("/api/js-visibility/pages", params={"website": "never-seen.example"}).json()
        self.assertEqual(body["pages_measured"], 0)
        self.assertEqual(body["pages"], [])


if __name__ == "__main__":
    unittest.main()
