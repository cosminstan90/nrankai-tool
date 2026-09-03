"""
Etapa 6 of the consolidation (docs/CONSOLIDATION_PLAN.md): tests for
core/technical_facts.py, the deterministic-facts module that feeds the
"Technical SEO Content Audit" prompt so the LLM judges instead of guessing
at things the tool can verify directly (robots.txt AI-crawler blocking,
/llms.txt presence, JSON-LD structured data).
"""
import unittest

from core.technical_facts import (
    _is_fully_blocked,
    _parse_robots_disallow,
    extract_structured_data_types,
    format_facts_block,
)


class TestParseRobotsDisallow(unittest.TestCase):
    def test_blocks_specific_agent(self):
        content = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin"
        rules = _parse_robots_disallow(content)
        self.assertTrue(_is_fully_blocked("GPTBot", rules))
        self.assertFalse(_is_fully_blocked("ClaudeBot", rules))

    def test_wildcard_blocks_everyone(self):
        content = "User-agent: *\nDisallow: /"
        rules = _parse_robots_disallow(content)
        self.assertTrue(_is_fully_blocked("ClaudeBot", rules))
        self.assertTrue(_is_fully_blocked("GPTBot", rules))

    def test_partial_disallow_is_not_fully_blocked(self):
        content = "User-agent: GPTBot\nDisallow: /private/"
        rules = _parse_robots_disallow(content)
        self.assertFalse(_is_fully_blocked("GPTBot", rules))

    def test_no_rules_means_allowed(self):
        rules = _parse_robots_disallow("")
        self.assertFalse(_is_fully_blocked("GPTBot", rules))


class TestExtractStructuredDataTypes(unittest.TestCase):
    def test_single_type(self):
        html = '<script type="application/ld+json">{"@type": "Article", "headline": "x"}</script>'
        self.assertEqual(extract_structured_data_types(html), ["Article"])

    def test_multiple_scripts(self):
        html = (
            '<script type="application/ld+json">{"@type": "Organization"}</script>'
            '<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>'
        )
        self.assertEqual(extract_structured_data_types(html), ["Organization", "BreadcrumbList"])

    def test_graph_structure(self):
        html = '<script type="application/ld+json">{"@graph": [{"@type": "Article"}, {"@type": "Person"}]}</script>'
        self.assertEqual(extract_structured_data_types(html), ["Article", "Person"])

    def test_list_of_types(self):
        html = '<script type="application/ld+json">{"@type": ["Product", "Offer"]}</script>'
        self.assertEqual(extract_structured_data_types(html), ["Product", "Offer"])

    def test_top_level_array(self):
        html = '<script type="application/ld+json">[{"@type": "FAQPage"}, {"@type": "WebPage"}]</script>'
        self.assertEqual(extract_structured_data_types(html), ["FAQPage", "WebPage"])

    def test_deduplicates(self):
        html = (
            '<script type="application/ld+json">{"@type": "Article"}</script>'
            '<script type="application/ld+json">{"@type": "Article"}</script>'
        )
        self.assertEqual(extract_structured_data_types(html), ["Article"])

    def test_malformed_json_is_skipped_not_fatal(self):
        html = '<script type="application/ld+json">{not valid json</script>'
        self.assertEqual(extract_structured_data_types(html), [])

    def test_no_jsonld_present(self):
        html = "<html><body><p>Just some text, no schema</p></body></html>"
        self.assertEqual(extract_structured_data_types(html), [])


class TestFormatFactsBlock(unittest.TestCase):
    def test_domain_facts_none_flags_unknown(self):
        block = format_facts_block(None, None)
        self.assertIn("could not be checked", block)
        self.assertIn("NONE detected", block)

    def test_robots_blocks_crawlers(self):
        facts = {"robots_txt_accessible": True, "ai_crawlers_blocked": ["GPTBot"], "ai_crawlers_allowed": [], "llms_txt_present": False}
        block = format_facts_block(facts, None)
        self.assertIn("BLOCKS these AI crawlers: GPTBot", block)
        self.assertIn("not found", block)

    def test_open_robots_and_llms_txt_present(self):
        facts = {"robots_txt_accessible": True, "ai_crawlers_blocked": [], "ai_crawlers_allowed": ["GPTBot"], "llms_txt_present": True}
        block = format_facts_block(facts, ["Article"])
        self.assertIn("does NOT block any known AI crawler", block)
        self.assertIn("present (200 OK)", block)
        self.assertIn("DETECTED on this page: Article", block)

    def test_robots_not_accessible(self):
        facts = {"robots_txt_accessible": False, "ai_crawlers_blocked": [], "ai_crawlers_allowed": [], "llms_txt_present": False}
        block = format_facts_block(facts, None)
        self.assertIn("not accessible/found", block)


if __name__ == "__main__":
    unittest.main()
