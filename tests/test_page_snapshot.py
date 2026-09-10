"""
Etapa 6 of docs/IMPROVEMENTS_PLAN.md -- content change detection.

Extraction runs on the HTML the scraper actually stores, which is
document.body only (core/web_scraper.py's DEEP_HTML_SCRIPT ends with
`getDeepHTML(document.body)`). Verified across 40 real stored pages: <title>,
meta description and rel=canonical appear in 0 of them, H1 in all 40. So this
stage extracts what is genuinely there; head metadata is a separate step.
"""
import unittest
from pathlib import Path

from core.page_snapshot import extract_page_fields

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestExtractPageFields(unittest.TestCase):
    def setUp(self):
        self.v1 = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/credit")

    def test_extracts_headings_in_document_order(self):
        self.assertEqual(self.v1["h1"], ["Credit ipotecar cu dobandă fixă"])
        self.assertEqual(self.v1["h2"], ["Cum funcționează", "Documente necesare"])

    def test_counts_words_from_visible_text_only(self):
        """Markup and attributes must not inflate the count."""
        self.assertGreater(self.v1["word_count"], 30)
        self.assertLess(self.v1["word_count"], 100)

    def test_separates_internal_from_external_links(self):
        self.assertIn("/persoane-fizice/credite", self.v1["internal_links"])
        self.assertIn("/persoane-fizice/simulator", self.v1["internal_links"])
        self.assertNotIn("https://example-extern.com/ghid", self.v1["internal_links"])

    def test_counts_images_missing_alt(self):
        self.assertEqual(self.v1["images_total"], 2)
        self.assertEqual(self.v1["images_without_alt"], 0)

    def test_detects_missing_alt_in_the_second_version(self):
        v2 = extract_page_fields(_html("credit_v2.html"), "https://ing.ro/credit")
        self.assertEqual(v2["images_without_alt"], 1)

    def test_collects_jsonld_types_present_in_the_body(self):
        v2 = extract_page_fields(_html("credit_v2.html"), "https://ing.ro/credit")
        self.assertEqual(v2["schema_types"], ["FAQPage"])
        self.assertEqual(self.v1["schema_types"], [])

    def test_content_hash_is_stable_for_identical_input(self):
        again = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/credit")
        self.assertEqual(self.v1["content_hash"], again["content_hash"])

    def test_content_hash_covers_head_metadata_too(self):
        """
        Caught end to end: compare_runs short-circuits on this hash, so a hash
        that ignored head fields made a title rewrite or a switch to noindex
        completely invisible whenever the body text was untouched -- which is
        exactly how a CMS metadata edit looks.
        """
        base = {"title": "Persoane fizice | ING", "meta_description": None,
                "canonical": None, "meta_robots": "index,follow", "jsonld": []}
        v1 = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/x", head_meta=base)
        retitled = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/x",
                                       head_meta={**base, "title": "Produse ING"})
        noindexed = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/x",
                                        head_meta={**base, "meta_robots": "noindex,follow"})

        self.assertNotEqual(v1["content_hash"], retitled["content_hash"])
        self.assertNotEqual(v1["content_hash"], noindexed["content_hash"])

    def test_content_hash_changes_when_the_page_changes(self):
        v2 = extract_page_fields(_html("credit_v2.html"), "https://ing.ro/credit")
        self.assertNotEqual(self.v1["content_hash"], v2["content_hash"])

    def test_head_metadata_fills_in_the_fields_the_body_cannot_provide(self):
        """
        The scraper now captures <head> separately (core/web_scraper.py's
        HEAD_META_SCRIPT), because title, meta description and canonical live
        there and appeared in 0 of 40 real stored pages otherwise.
        """
        head = {
            "title": "Credit ipotecar | ING",
            "meta_description": "Dobandă fixă pe toată durata.",
            "canonical": "https://ing.ro/credit",
            "meta_robots": "index,follow",
            "jsonld": [],
        }
        fields = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/credit", head_meta=head)

        self.assertEqual(fields["title"], "Credit ipotecar | ING")
        self.assertEqual(fields["meta_description"], "Dobandă fixă pe toată durata.")
        self.assertEqual(fields["canonical"], "https://ing.ro/credit")
        self.assertEqual(fields["meta_robots"], "index,follow")

    def test_head_jsonld_is_merged_without_duplicating_body_blocks(self):
        """
        Both sources are read, but a block present in both must be counted
        once -- browsers relocate ld+json from head into body while parsing,
        so the same schema legitimately shows up twice.
        """
        head = {"title": None, "meta_description": None, "canonical": None,
                "meta_robots": None,
                "jsonld": ['{"@context":"https://schema.org","@type":"FAQPage"}',
                           '{"@context":"https://schema.org","@type":"Organization"}']}
        fields = extract_page_fields(_html("credit_v2.html"), "https://ing.ro/credit", head_meta=head)

        # credit_v2.html already carries FAQPage in its body
        self.assertEqual(sorted(fields["schema_types"]), ["FAQPage", "Organization"])

    def test_absent_head_metadata_still_reads_as_not_captured(self):
        """Pages scraped before head capture existed have no sidecar."""
        fields = extract_page_fields(_html("credit_v1.html"), "https://ing.ro/credit", head_meta=None)
        self.assertIsNone(fields["title"])
        self.assertIsNone(fields["meta_robots"])

    def test_fields_absent_from_body_only_html_are_none_not_empty_string(self):
        """
        The scraper stores document.body, so title/meta/canonical are simply
        not in the data -- 0 of 40 real pages had them. They must read as
        "unknown" (None), never as "" which a diff would report as
        "title was removed" on every single page.
        """
        self.assertIsNone(self.v1["title"])
        self.assertIsNone(self.v1["meta_description"])
        self.assertIsNone(self.v1["canonical"])


if __name__ == "__main__":
    unittest.main()
