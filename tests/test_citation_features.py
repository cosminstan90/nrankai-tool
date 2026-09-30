"""
Pasul 16 of docs/superpowers/plans/2026-09-30-next-steps.md.

Fixture HTML per feature, as the plan asks for -- core.citation_features is
pure extraction with no network access.
"""
import unittest

from core.citation_features import extract_features

BASE_LONG_TEXT = " ".join(["lorem ipsum dolor sit amet"] * 30)   # padding past extract_content's 100-char floor


class TestWordCountAndDirectAnswer(unittest.TestCase):
    def test_word_count_reflects_body_text(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        features = extract_features(html)
        self.assertGreater(features["word_count"], 100)

    def test_a_direct_answer_in_the_opening_words_is_detected(self):
        html = f"<html><body><p>Credit ipotecar cu dobanda fixa {BASE_LONG_TEXT}</p></body></html>"
        features = extract_features(html, query="credit ipotecar dobanda fixa")
        self.assertTrue(features["direct_answer_in_first_words"])

    def test_the_answer_buried_far_down_is_not_a_direct_answer(self):
        filler = " ".join(["cuvant"] * 150)
        html = f"<html><body><p>{filler} credit ipotecar cu dobanda fixa</p></body></html>"
        features = extract_features(html, query="credit ipotecar dobanda fixa")
        self.assertFalse(features["direct_answer_in_first_words"])

    def test_no_query_given_omits_the_direct_answer_feature(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        features = extract_features(html)
        self.assertNotIn("direct_answer_in_first_words", features)


class TestTablesAndLists(unittest.TestCase):
    def test_a_table_is_detected(self):
        html = f"<html><body><table><tr><td>x</td></tr></table><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertTrue(extract_features(html)["has_tables"])

    def test_no_table_is_correctly_absent(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertFalse(extract_features(html)["has_tables"])

    def test_an_unordered_list_is_detected(self):
        html = f"<html><body><ul><li>a</li><li>b</li></ul><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertTrue(extract_features(html)["has_lists"])

    def test_an_ordered_list_is_detected(self):
        html = f"<html><body><ol><li>a</li><li>b</li></ol><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertTrue(extract_features(html)["has_lists"])


class TestQaContent(unittest.TestCase):
    def test_three_or_more_questions_count_as_qa_content(self):
        html = (f"<html><body><p>Ce acte imi trebuie? Cat dureaza aprobarea? "
               f"Pot rambursa anticipat? {BASE_LONG_TEXT}</p></body></html>")
        self.assertTrue(extract_features(html)["has_qa_content"])

    def test_one_question_is_not_enough_to_count_as_qa_content(self):
        html = f"<html><body><p>Ce acte imi trebuie? {BASE_LONG_TEXT}</p></body></html>"
        self.assertFalse(extract_features(html)["has_qa_content"])


class TestNumericDensity(unittest.TestCase):
    def test_a_page_with_prices_and_percentages_has_nonzero_density(self):
        html = f"<html><body><p>Rata este 6.5% iar suma maxima 500000 lei. {BASE_LONG_TEXT}</p></body></html>"
        self.assertGreater(extract_features(html)["numeric_density_per_100_words"], 0)

    def test_a_page_with_no_figures_has_zero_density(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertEqual(extract_features(html)["numeric_density_per_100_words"], 0.0)


class TestJsonLdTypes(unittest.TestCase):
    def test_a_financial_product_schema_is_detected(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "FinancialProduct", "name": "Credit ipotecar"}'
            f'</script><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        self.assertIn("FinancialProduct", extract_features(html)["json_ld_types"])

    def test_no_json_ld_is_an_empty_list_not_none(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertEqual(extract_features(html)["json_ld_types"], [])


class TestAuthor(unittest.TestCase):
    def test_a_meta_author_tag_is_detected(self):
        html = f'<html><head><meta name="author" content="Ana Pop"></head><body><p>{BASE_LONG_TEXT}</p></body></html>'
        self.assertTrue(extract_features(html)["has_author"])

    def test_a_json_ld_author_is_detected(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Article", "author": {"@type": "Person", "name": "Ana Pop"}}'
            f'</script><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        self.assertTrue(extract_features(html)["has_author"])

    def test_no_author_signal_is_correctly_absent(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        self.assertFalse(extract_features(html)["has_author"])


class TestHeadingStructure(unittest.TestCase):
    def test_heading_counts_are_correct(self):
        html = f"<html><body><h1>T</h1><h2>A</h2><h2>B</h2><p>{BASE_LONG_TEXT}</p></body></html>"
        structure = extract_features(html)["heading_structure"]
        self.assertEqual(structure["h1"], 1)
        self.assertEqual(structure["h2"], 2)
        self.assertEqual(structure["h3"], 0)


class TestDates(unittest.TestCase):
    def test_a_meta_published_time_is_extracted(self):
        html = (
            '<html><head><meta property="article:published_time" content="2026-01-15T10:00:00Z"></head>'
            f'<body><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        self.assertEqual(extract_features(html)["published_date"], "2026-01-15T10:00:00Z")

    def test_a_json_ld_date_modified_is_extracted(self):
        html = (
            '<html><body><script type="application/ld+json">'
            '{"@type": "Article", "dateModified": "2026-06-01T00:00:00Z"}'
            f'</script><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        self.assertEqual(extract_features(html)["updated_date"], "2026-06-01T00:00:00Z")

    def test_no_date_anywhere_is_none_not_a_guess(self):
        html = f"<html><body><p>{BASE_LONG_TEXT}</p></body></html>"
        features = extract_features(html)
        self.assertIsNone(features["published_date"])
        self.assertIsNone(features["updated_date"])
        self.assertIsNone(features["days_since_updated"])

    def test_a_trailing_slash_from_an_unquoted_meta_attribute_is_tolerated(self):
        """
        Found live on a real ing.ro page: an unquoted meta content value
        folds the tag's own self-closing "/" into the parsed attribute
        (content=2025-04-30T10:38:46.750+03:00/>). Must still parse.
        """
        html = (
            '<html><head><meta property="article:modified_time" '
            f'content=2026-01-15T10:00:00+03:00/></head><body><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        features = extract_features(html)
        self.assertIsNotNone(features["days_since_updated"])

    def test_days_since_updated_is_a_small_number_for_a_recent_date(self):
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        html = (
            f'<html><head><meta property="article:modified_time" content="{recent}"></head>'
            f'<body><p>{BASE_LONG_TEXT}</p></body></html>'
        )
        days = extract_features(html)["days_since_updated"]
        self.assertIsNotNone(days)
        self.assertLessEqual(days, 6)


if __name__ == "__main__":
    unittest.main()
