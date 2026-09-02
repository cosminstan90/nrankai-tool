"""
Pins the fix from Etapa 5.1 of the consolidation (docs/CONSOLIDATION_PLAN.md):
api/routes/action_cards.py's CSV export did action.get("current", "")[:200].
dict.get(key, default) only substitutes the default when the KEY is absent,
not when its value is None -- and the LLM prompt in this same file
(line ~219) explicitly instructs the model to return "current": null when no
current text exists, with another code path in the same file (line ~316)
constructing that value directly. So a normal, expected action card crashed
export_csv() with TypeError: 'NoneType' object is not subscriptable.
"""
import unittest

from api.routes.action_cards import export_csv


class _FakeCard:
    def __init__(self, actions_json):
        self.page_url = "https://example.com/page1"
        self.page_title = "Example Page"
        self.priority = "critical"
        self.current_score = 45.0
        self.target_score = 70.0
        self.completed_actions = 0
        self.total_actions = 1
        self.actions_json = actions_json


class _FakeAudit:
    website = "https://example.com"


class TestExportCsvHandlesNullFields(unittest.IsolatedAsyncioTestCase):
    async def test_null_current_does_not_crash(self):
        import json
        card = _FakeCard(json.dumps([
            {"id": 1, "category": "meta", "action": "Add meta description",
             "current": None, "recommended": "New description",
             "reason": "Missing", "difficulty": "easy", "completed": False},
        ]))
        response = await export_csv([card], _FakeAudit())
        body = response.body.decode("utf-8")
        self.assertIn("Add meta description", body)

    async def test_null_recommended_and_reason_do_not_crash(self):
        import json
        card = _FakeCard(json.dumps([
            {"id": 1, "category": "meta", "action": "Fix title",
             "current": "Old title", "recommended": None,
             "reason": None, "difficulty": "medium", "completed": True},
        ]))
        response = await export_csv([card], _FakeAudit())
        body = response.body.decode("utf-8")
        self.assertIn("Fix title", body)


if __name__ == "__main__":
    unittest.main()
