"""
Pasul 16 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.citation_comparison is pure logic on already-extracted feature dicts --
no HTML, no network.
"""
import unittest

from core.citation_comparison import build_recommendation_prompt, compare_to_citations


def _cited(has_tables=False, has_lists=False, has_qa_content=False, has_author=False,
          direct_answer=False, density=0.0, days_since_updated=None, json_ld_types=None):
    return {
        "has_tables": has_tables, "has_lists": has_lists, "has_qa_content": has_qa_content,
        "has_author": has_author, "direct_answer_in_first_words": direct_answer,
        "numeric_density_per_100_words": density, "days_since_updated": days_since_updated,
        "json_ld_types": json_ld_types or [],
    }


class TestCompareToCitationsNoSample(unittest.TestCase):
    def test_zero_cited_pages_is_a_small_sample_with_no_findings(self):
        result = compare_to_citations(_cited(), [])
        self.assertEqual(result["sample_size"], 0)
        self.assertTrue(result["small_sample"])
        self.assertEqual(result["findings"], [])


class TestCompareToCitationsBooleanFeatures(unittest.TestCase):
    def test_a_feature_most_cited_pages_have_and_own_page_lacks_is_a_finding(self):
        cited = [_cited(has_tables=True), _cited(has_tables=True), _cited(has_tables=False)]
        result = compare_to_citations(_cited(has_tables=False), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertIn("has_tables", features)

    def test_a_feature_most_cited_pages_have_but_own_page_also_has_is_not_a_finding(self):
        cited = [_cited(has_tables=True), _cited(has_tables=True)]
        result = compare_to_citations(_cited(has_tables=True), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertNotIn("has_tables", features)

    def test_a_feature_only_a_minority_of_cited_pages_have_is_not_a_finding(self):
        cited = [_cited(has_tables=True), _cited(has_tables=False), _cited(has_tables=False)]
        result = compare_to_citations(_cited(has_tables=False), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertNotIn("has_tables", features)

    def test_small_sample_is_flagged_but_still_reports_findings(self):
        cited = [_cited(has_tables=True)]
        result = compare_to_citations(_cited(has_tables=False), cited)
        self.assertTrue(result["small_sample"])
        features = {f["feature"] for f in result["findings"]}
        self.assertIn("has_tables", features)


class TestCompareToCitationsNumericDensity(unittest.TestCase):
    def test_a_much_lower_density_than_cited_pages_is_a_finding(self):
        cited = [_cited(density=5.0), _cited(density=6.0), _cited(density=5.5)]
        result = compare_to_citations(_cited(density=0.5), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertIn("numeric_density_per_100_words", features)

    def test_a_comparable_density_is_not_a_finding(self):
        cited = [_cited(density=5.0), _cited(density=5.0)]
        result = compare_to_citations(_cited(density=4.5), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertNotIn("numeric_density_per_100_words", features)


class TestCompareToCitationsRecency(unittest.TestCase):
    def test_most_cited_pages_recently_updated_and_own_page_unknown_is_a_finding(self):
        cited = [_cited(days_since_updated=30), _cited(days_since_updated=60)]
        result = compare_to_citations(_cited(days_since_updated=None), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertIn("days_since_updated", features)

    def test_most_cited_pages_recently_updated_and_own_page_stale_is_a_finding(self):
        cited = [_cited(days_since_updated=30), _cited(days_since_updated=60)]
        result = compare_to_citations(_cited(days_since_updated=900), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertIn("days_since_updated", features)

    def test_own_page_also_recently_updated_is_not_a_finding(self):
        cited = [_cited(days_since_updated=30), _cited(days_since_updated=60)]
        result = compare_to_citations(_cited(days_since_updated=45), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertNotIn("days_since_updated", features)

    def test_cited_pages_themselves_mostly_stale_is_not_a_finding(self):
        cited = [_cited(days_since_updated=900), _cited(days_since_updated=1000)]
        result = compare_to_citations(_cited(days_since_updated=None), cited)
        features = {f["feature"] for f in result["findings"]}
        self.assertNotIn("days_since_updated", features)


class TestCompareToCitationsJsonLd(unittest.TestCase):
    def test_a_schema_type_most_cited_pages_have_is_a_finding(self):
        cited = [_cited(json_ld_types=["FinancialProduct"]), _cited(json_ld_types=["FinancialProduct"])]
        result = compare_to_citations(_cited(json_ld_types=[]), cited)
        labels = {f["feature"] for f in result["findings"]}
        self.assertIn("json_ld_types", labels)

    def test_a_schema_type_the_own_page_already_has_is_not_a_finding(self):
        cited = [_cited(json_ld_types=["FinancialProduct"]), _cited(json_ld_types=["FinancialProduct"])]
        result = compare_to_citations(_cited(json_ld_types=["FinancialProduct"]), cited)
        labels = {f["feature"] for f in result["findings"]}
        self.assertNotIn("json_ld_types", labels)


class TestBuildRecommendationPrompt(unittest.TestCase):
    def test_no_findings_returns_none(self):
        report = {"sample_size": 2, "small_sample": True, "findings": []}
        self.assertIsNone(build_recommendation_prompt("credit ipotecar", report))

    def test_findings_produce_a_prompt_with_the_query_and_features(self):
        report = {"sample_size": 3, "small_sample": False, "findings": [
            {"feature": "has_tables", "label": "a comparison table", "cited_pages_with_it": 2, "cited_pages_total": 3},
        ]}
        prompt = build_recommendation_prompt("credit ipotecar", report)
        self.assertIn("credit ipotecar", prompt)
        self.assertIn("comparison table", prompt)

    def test_small_sample_is_flagged_in_the_prompt_text(self):
        report = {"sample_size": 1, "small_sample": True, "findings": [
            {"feature": "has_tables", "label": "a comparison table", "cited_pages_with_it": 1, "cited_pages_total": 1},
        ]}
        prompt = build_recommendation_prompt("q", report)
        self.assertIn("small sample", prompt.lower())


if __name__ == "__main__":
    unittest.main()
