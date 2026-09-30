"""
Pasul 17 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.passages is pure HTML chunking, no network access.
"""
import unittest

from core.passages import MAX_PASSAGE_WORDS, MIN_PASSAGE_WORDS, chunk_html_into_passages


def _words(n, start=0):
    return " ".join(f"word{i}" for i in range(start, start + n))


class TestChunkHtmlIntoPassages(unittest.TestCase):
    def test_empty_page_produces_no_passages(self):
        self.assertEqual(chunk_html_into_passages("<html><body></body></html>"), [])

    def test_a_short_page_is_one_passage(self):
        html = f"<html><body><p>{_words(50)}</p></body></html>"
        passages = chunk_html_into_passages(html)
        self.assertEqual(len(passages), 1)
        self.assertEqual(len(passages[0].split()), 50)

    def test_a_heading_starts_a_new_passage_once_the_current_one_has_real_content(self):
        html = (
            f"<html><body><h2>Section A</h2><p>{_words(200, 0)}</p>"
            f"<h2>Section B</h2><p>{_words(200, 200)}</p></body></html>"
        )
        passages = chunk_html_into_passages(html)
        self.assertEqual(len(passages), 2)
        self.assertIn("Section A", passages[0])
        self.assertIn("Section B", passages[1])

    def test_a_heading_immediately_followed_by_another_heading_does_not_fragment(self):
        """No real content between two headings yet -- must not create a near-empty passage."""
        html = f"<html><body><h2>A</h2><h3>B</h3><p>{_words(200)}</p></body></html>"
        passages = chunk_html_into_passages(html)
        self.assertEqual(len(passages), 1)

    def test_an_overly_long_section_is_split_by_word_count(self):
        html = f"<html><body><h2>One long section</h2><p>{_words(700)}</p></body></html>"
        passages = chunk_html_into_passages(html)
        self.assertGreaterEqual(len(passages), 2)
        for p in passages[:-1]:
            self.assertLessEqual(len(p.split()), MAX_PASSAGE_WORDS)

    def test_div_based_content_with_no_p_tags_is_still_extracted(self):
        """
        Found live on a real ing.ro page: real body content lived entirely
        in plain <div>s, not <p> tags (7 <p> tags on that page were all
        boilerplate/cookie-consent text). Walking <p>/<li> tags directly
        missed the real content; must extract via the page's whole-body text
        instead (core.html2llm_converter.extract_content), same as this test.
        """
        html = f'<html><body><h2>Titlu</h2><div>{_words(200)}</div></body></html>'
        passages = chunk_html_into_passages(html)
        self.assertEqual(len(passages), 1)
        self.assertGreaterEqual(len(passages[0].split()), 200)

    def test_script_and_style_content_is_excluded(self):
        html = (
            f"<html><body><script>var x = 'should not appear';</script>"
            f"<style>.a {{ color: red; }}</style><p>{_words(160)}</p></body></html>"
        )
        passages = chunk_html_into_passages(html)
        combined = " ".join(passages)
        self.assertNotIn("should not appear", combined)


if __name__ == "__main__":
    unittest.main()
