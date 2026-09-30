"""
Pasul 17 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.fanout_coverage is pure logic on already-embedded vectors -- no
network, no provider call. TestThresholdCalibration reproduces the exact
real (query, passage) similarity values the thresholds were calibrated
against (see core/fanout_coverage.py's module docstring for the real
queries and page), using synthetic 2D vectors engineered to have that exact
cosine similarity, so the calibration is checked without a real API call.
"""
import math
import unittest

from core.fanout_coverage import (
    build_coverage_report, classify_coverage, find_best_passage,
)


def _vector_at_similarity(target_similarity: float):
    """A 2D unit vector whose cosine similarity with [1, 0] is exactly target_similarity."""
    angle = math.acos(target_similarity)
    return [math.cos(angle), math.sin(angle)]


REFERENCE = [1.0, 0.0]


class TestThresholdCalibration(unittest.TestCase):
    """
    The four real calibration points (text-embedding-3-small, a real ing.ro
    page on choosing a credit, chunked by core/passages.py):
      0.5352 "cum aleg creditul potrivit pentru mine"   -- clearly on-topic
      0.5146 "ce este dobanda variabila la un credit"   -- related, on-topic
      0.4501 "cum deschid un cont curent la banca"      -- banking-adjacent, off-topic
      0.3159 "retete de prajituri traditionale"         -- clearly unrelated
    """
    def test_the_clearly_on_topic_query_is_covered(self):
        self.assertEqual(classify_coverage(0.5352), "covered")

    def test_the_related_on_topic_query_is_covered(self):
        self.assertEqual(classify_coverage(0.5146), "covered")

    def test_the_banking_adjacent_off_topic_query_is_weak(self):
        self.assertEqual(classify_coverage(0.4501), "weak")

    def test_the_clearly_unrelated_query_is_uncovered(self):
        self.assertEqual(classify_coverage(0.3159), "uncovered")

    def test_find_best_passage_reproduces_the_on_topic_similarity(self):
        query_vector = REFERENCE
        passages = [{"url": "/a", "text": "x", "vector": _vector_at_similarity(0.5352)}]
        best = find_best_passage(query_vector, passages)
        self.assertAlmostEqual(best["similarity"], 0.5352, places=3)
        self.assertEqual(classify_coverage(best["similarity"]), "covered")


class TestClassifyCoverageBoundaries(unittest.TestCase):
    def test_no_passage_at_all_is_uncovered_not_an_error(self):
        self.assertEqual(classify_coverage(None), "uncovered")

    def test_a_perfect_match_is_covered(self):
        self.assertEqual(classify_coverage(1.0), "covered")

    def test_zero_similarity_is_uncovered(self):
        self.assertEqual(classify_coverage(0.0), "uncovered")


class TestFindBestPassage(unittest.TestCase):
    def test_the_highest_similarity_passage_wins(self):
        query_vector = [1.0, 0.0]
        passages = [
            {"url": "/low", "text": "x", "vector": [0.0, 1.0]},
            {"url": "/high", "text": "y", "vector": [0.99, 0.14]},
        ]
        best = find_best_passage(query_vector, passages)
        self.assertEqual(best["url"], "/high")

    def test_no_passages_is_none_not_an_error(self):
        self.assertIsNone(find_best_passage([1.0, 0.0], []))


class TestBuildCoverageReport(unittest.TestCase):
    def _passages(self):
        return [{"url": "/on-topic", "text": "x", "vector": _vector_at_similarity(1.0)}]

    def test_a_covered_query_is_not_in_the_gaps(self):
        sub_queries = [{"query": "covered query", "vector": REFERENCE, "cluster": "c1"}]
        report = build_coverage_report(sub_queries, self._passages())
        self.assertEqual(report["covered_count"], 1)
        self.assertEqual(report["gaps_by_cluster"], {})

    def test_an_uncovered_query_appears_in_its_cluster(self):
        orthogonal = [0.0, 1.0]   # similarity 0.0 with the passage's vector at similarity 1.0 to REFERENCE
        sub_queries = [{"query": "uncovered query", "vector": orthogonal, "cluster": "c2"}]
        report = build_coverage_report(sub_queries, self._passages())
        self.assertEqual(report["uncovered_count"], 1)
        self.assertIn("c2", report["gaps_by_cluster"])
        self.assertEqual(report["gaps_by_cluster"]["c2"][0]["query"], "uncovered query")

    def test_a_query_with_no_cluster_groups_as_uncategorized(self):
        orthogonal = [0.0, 1.0]
        sub_queries = [{"query": "uncovered query", "vector": orthogonal}]   # no "cluster" key
        report = build_coverage_report(sub_queries, self._passages())
        self.assertIn("uncategorized", report["gaps_by_cluster"])

    def test_a_site_with_no_passages_at_all_reports_every_query_uncovered(self):
        sub_queries = [{"query": "q1", "vector": REFERENCE}, {"query": "q2", "vector": REFERENCE}]
        report = build_coverage_report(sub_queries, [])
        self.assertEqual(report["uncovered_count"], 2)
        self.assertEqual(report["covered_count"], 0)
        for r in report["queries"]:
            self.assertIsNone(r["closest_url"])


if __name__ == "__main__":
    unittest.main()
