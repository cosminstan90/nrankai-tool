"""
Pins the fix from Etapa 4.2 of the consolidation (docs/CONSOLIDATION_PLAN.md):
api/routes/projects.py used to link a FanoutSession to a FanoutProject by
writing the project's UUID into FanoutSession.audit_id -- a column declared
as a real ForeignKey to the unrelated `audits` table. It only worked because
PRAGMA foreign_keys=ON is applied to sync_engine only, never to the async
engine every real request uses. FanoutSession.project_id is the dedicated
column that replaces that reuse (migrations/versions/
0011_fanout_session_project_id.py).
"""
import unittest

from api.models.content import FanoutSession


class TestFanoutSessionProjectId(unittest.TestCase):
    def test_project_id_is_a_distinct_column_from_audit_id(self):
        columns = FanoutSession.__table__.columns
        self.assertIn("project_id", columns)
        self.assertIn("audit_id", columns)
        self.assertIsNot(columns["project_id"], columns["audit_id"])

    def test_project_id_has_no_foreign_key(self):
        """Unlike audit_id (a real FK to `audits`), project_id must stay a
        plain nullable column -- matching the existing, unenforced convention
        already used by FanoutTrackingConfig.project_id and
        FanoutCompetitiveReport.project_id. A ForeignKey→fanout_projects here
        would be more correct in principle, but async request handling
        doesn't enforce FKs at all (PRAGMA foreign_keys=ON is sync-engine-only
        — see api/models/database.py), so adding one now would only add false
        confidence, not real integrity."""
        project_id_col = FanoutSession.__table__.columns["project_id"]
        self.assertEqual(len(project_id_col.foreign_keys), 0)

    def test_audit_id_still_has_its_real_foreign_key(self):
        """Confirms the fix didn't accidentally touch the legitimate FK."""
        audit_id_col = FanoutSession.__table__.columns["audit_id"]
        self.assertEqual(len(audit_id_col.foreign_keys), 1)
        fk = next(iter(audit_id_col.foreign_keys))
        self.assertEqual(fk.column.table.name, "audits")

    def test_to_dict_includes_project_id(self):
        session = FanoutSession(
            id="s1", prompt="p", provider="openai", model="gpt-4o",
            project_id="proj-123",
        )
        data = session.to_dict()
        self.assertEqual(data["project_id"], "proj-123")
        self.assertIsNone(data["audit_id"])


if __name__ == "__main__":
    unittest.main()
