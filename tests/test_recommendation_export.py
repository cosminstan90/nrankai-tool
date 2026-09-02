"""
Pins the fix from Etapa 5.1 of the consolidation (docs/CONSOLIDATION_PLAN.md):
action_cards.py and content_briefs.py both duplicated CSV/HTML/Trello export
logic for a "list of page-level recommendations" -- action_cards.py had all
4 formats, content_briefs.py had only JSON. api/utils/recommendation_export.py
unifies the presentation layer (not generation -- the two features keep their
own separate prompts and output schemas) so content_briefs.py gained CSV/
HTML/Trello for free, and action_cards.py's export fidelity (Category column,
progress badge, page_title fallback, difficulty/impact color coding) is
pinned here so a future refactor can't silently drop a field again.
"""
import unittest

from api.utils.recommendation_export import (
    ExportItem,
    ExportPage,
    build_csv_response,
    build_html_response,
    build_trello_export,
)


def _sample_page(**overrides) -> ExportPage:
    defaults = dict(
        page_url="https://example.com/page1",
        page_title="Example Page",
        priority="critical",
        current_score=45.0,
        target_score=70.0,
        progress_label="1/3",
        items=[
            ExportItem(
                title="Add meta description",
                category="meta",
                tag="easy",
                current=None,
                recommended="New description",
                reason="Missing entirely",
                completed=False,
            ),
        ],
    )
    defaults.update(overrides)
    return ExportPage(**defaults)


class TestCsvExport(unittest.TestCase):
    def test_header_includes_category_and_tag_columns(self):
        response = build_csv_response([_sample_page()], "example.com", "test")
        body = response.body.decode("utf-8")
        header = body.splitlines()[0]
        self.assertIn("Category", header)
        self.assertIn("Tag", header)

    def test_null_current_and_recommended_do_not_crash(self):
        page = _sample_page(items=[
            ExportItem(title="X", current=None, recommended=None, reason=None),
        ])
        response = build_csv_response([page], "example.com", "test")
        body = response.body.decode("utf-8")
        self.assertIn("X", body)

    def test_row_data_present(self):
        response = build_csv_response([_sample_page()], "example.com", "test")
        body = response.body.decode("utf-8")
        self.assertIn("Add meta description", body)
        self.assertIn("meta", body)
        self.assertIn("easy", body)


class TestTrelloExport(unittest.TestCase):
    def test_groups_by_priority(self):
        result = build_trello_export([_sample_page(priority="critical")], "example.com", "Test")
        critical_list = next(l for l in result["lists"] if "Critical" in l["name"])
        self.assertEqual(len(critical_list["cards"]), 1)

    def test_unknown_priority_falls_back_to_medium_not_keyerror(self):
        page = _sample_page(priority="unexpected-value")
        result = build_trello_export([page], "example.com", "Test")
        medium_list = next(l for l in result["lists"] if "Medium" in l["name"])
        self.assertEqual(len(medium_list["cards"]), 1)

    def test_card_title_prefers_page_title(self):
        result = build_trello_export([_sample_page()], "example.com", "Test")
        card = result["lists"][0]["cards"][0]
        self.assertIn("Example Page", card["name"])

    def test_card_title_falls_back_to_url_without_page_title(self):
        page = _sample_page(page_title=None)
        result = build_trello_export([page], "example.com", "Test")
        card = result["lists"][0]["cards"][0]
        self.assertIn("https://example.com/page1", card["name"])


class TestHtmlExport(unittest.TestCase):
    def test_renders_without_crashing_on_missing_optional_fields(self):
        page = _sample_page(current_score=None, target_score=None, progress_label=None)
        response = build_html_response([page], "example.com", "Test", "test")
        self.assertEqual(response.media_type, "text/html")

    def test_difficulty_color_coding_present(self):
        page = _sample_page(items=[
            ExportItem(title="Easy one", tag="easy"),
            ExportItem(title="Hard one", tag="hard"),
        ])
        response = build_html_response([page], "example.com", "Test", "test")
        html = response.body.decode("utf-8")
        self.assertIn("#D1FAE5", html)  # easy = green
        self.assertIn("#FEE2E2", html)  # hard = red

    def test_impact_vocabulary_also_colored(self):
        """content_briefs uses impact (critical/high/medium), not difficulty --
        the color lookup must work for both vocabularies."""
        page = _sample_page(items=[ExportItem(title="X", tag="critical")])
        response = build_html_response([page], "example.com", "Test", "test")
        html = response.body.decode("utf-8")
        self.assertIn("#FEE2E2", html)  # critical = red, same as hard


if __name__ == "__main__":
    unittest.main()
