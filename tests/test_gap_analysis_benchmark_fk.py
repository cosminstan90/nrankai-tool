"""
Pins the fix from Etapa 5.2 of the consolidation (docs/CONSOLIDATION_PLAN.md):
CompetitorGapAnalysis.benchmark_id was a real FK to BenchmarkProject, but
creating a gap analysis with a benchmark_id never actually derived
target_audit_id/competitor_audit_ids from the linked benchmark -- the
caller had to redundantly re-supply both, making the FK purely decorative.
Confirmed live: creating a gap analysis with ONLY benchmark_id (no
target_audit_id/competitor_audit_ids) now correctly derives both from the
linked BenchmarkProject, matching it exactly.

This test pins the request-schema shape (both fields now Optional) rather
than re-running the full live flow, since that requires a running server
and real audits -- see the session's live verification for the full
end-to-end proof.
"""
import unittest

from api.routes.gap_analysis import GenerateGapAnalysisRequest


class TestGenerateGapAnalysisRequestSchema(unittest.TestCase):
    def test_benchmark_id_alone_is_valid(self):
        """A request with only benchmark_id (no explicit audit IDs) must
        validate -- the route derives the IDs from the benchmark."""
        req = GenerateGapAnalysisRequest(name="Test", benchmark_id="some-benchmark-id")
        self.assertIsNone(req.target_audit_id)
        self.assertIsNone(req.competitor_audit_ids)
        self.assertEqual(req.benchmark_id, "some-benchmark-id")

    def test_explicit_audit_ids_still_valid_without_benchmark(self):
        """Existing callers that always sent explicit IDs must keep working
        unchanged."""
        req = GenerateGapAnalysisRequest(
            name="Test",
            target_audit_id="target-1",
            competitor_audit_ids=["comp-1", "comp-2"],
        )
        self.assertEqual(req.target_audit_id, "target-1")
        self.assertEqual(req.competitor_audit_ids, ["comp-1", "comp-2"])
        self.assertIsNone(req.benchmark_id)

    def test_both_benchmark_and_explicit_ids_valid(self):
        """Explicit IDs alongside a benchmark_id must also validate -- the
        route lets explicit values win over derivation in that case."""
        req = GenerateGapAnalysisRequest(
            name="Test",
            target_audit_id="target-1",
            competitor_audit_ids=["comp-1"],
            benchmark_id="some-benchmark-id",
        )
        self.assertEqual(req.target_audit_id, "target-1")
        self.assertEqual(req.benchmark_id, "some-benchmark-id")


if __name__ == "__main__":
    unittest.main()
