"""
Unit tests for Content Chunker module.

Tests cover:
- Short content (fits in one chunk)
- Content at exact limit
- Content slightly over limit (smart truncation)
- Very long content (multi-chunk)
- Non-English content (Dutch compound words)
- Audit result merging

Author: Cosmin (via Claude)
Created: 2026-02-11
"""

import unittest
import json
from content_chunker import (
    ContentChunker,
    ChunkResult,
    ChunkMetadata,
    AuditResultMerger,
    PROVIDER_LIMITS,
    LANGUAGE_CHAR_PER_TOKEN,
    chunk_content,
    merge_chunk_results,
)


class TestContentChunker(unittest.TestCase):
    """Test cases for ContentChunker class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.chunker = ContentChunker(provider="ANTHROPIC")
        
    # =========================================================================
    # Test Case 1: Short content (fits in one chunk)
    # =========================================================================
    def test_short_content_single_chunk(self):
        """Test that short content returns as a single chunk without modification."""
        short_text = """## Introduction

This is a short page about banking services.

## Services

We offer checking and savings accounts.

## Contact

Call us at 1-800-BANK."""

        result = self.chunker.chunk_content(short_text)
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        self.assertEqual(result.chunks[0], short_text)
        self.assertEqual(result.metadata[0].original_length, len(short_text))
        self.assertEqual(result.metadata[0].chunk_length, len(short_text))
        self.assertFalse(result.metadata[0].is_truncated)
        self.assertEqual(result.metadata[0].truncated_chars, 0)
    
    def test_empty_content(self):
        """Test handling of empty content."""
        result = self.chunker.chunk_content("")
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        self.assertEqual(result.chunks[0], "")
        self.assertEqual(result.metadata[0].original_length, 0)
    
    # =========================================================================
    # Test Case 2: Content at exact limit
    # =========================================================================
    def test_content_at_exact_limit(self):
        """Test content that exactly matches the character limit."""
        # Create content exactly at limit
        limit = 5000
        exact_text = "A" * limit
        
        result = self.chunker.chunk_content(exact_text, max_chars=limit)
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        self.assertEqual(len(result.chunks[0]), limit)
        self.assertFalse(result.metadata[0].is_truncated)
    
    def test_content_just_under_limit(self):
        """Test content just under the character limit."""
        limit = 5000
        text = "A" * (limit - 1)
        
        result = self.chunker.chunk_content(text, max_chars=limit)
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        self.assertEqual(len(result.chunks[0]), limit - 1)
    
    # =========================================================================
    # Test Case 3: Content slightly over limit (smart truncation)
    # =========================================================================
    def test_slightly_over_limit_smart_truncation(self):
        """Test that slightly oversized content gets smart-truncated."""
        # Content about 1.5x the limit - should trigger smart truncation (not multi-chunk)
        limit = 500
        
        # Create structured content with clear sections - about 1.5x limit (750 chars)
        text = """## Introduction

This is the introduction section with important context about the topic. It provides background information.

## Main Content

This section contains the main body of the content. It has multiple paragraphs with detailed information.

Here is another paragraph with more details about the subject matter being discussed here.

## Middle Section

This is a middle section with additional information. Lorem ipsum dolor sit amet consectetur adipiscing.

## Conclusion

This is the conclusion with the key takeaways and call to action for readers."""

        # Verify content is between 1x and 2x the limit
        self.assertGreater(len(text), limit)
        self.assertLess(len(text), limit * 2)
        
        result = self.chunker.chunk_content(text, max_chars=limit)
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        self.assertTrue(result.metadata[0].is_truncated)
        self.assertGreater(result.metadata[0].truncated_chars, 0)
        self.assertLessEqual(len(result.chunks[0]), limit)
        
        # Verify truncation note is present
        self.assertIn("[Content truncated:", result.chunks[0])
    
    def test_truncation_at_sentence_boundary(self):
        """Test that truncation happens at sentence boundaries."""
        limit = 200
        text = "This is the first sentence. This is the second sentence. This is the third sentence which is quite a bit longer."
        
        truncated, _ = self.chunker.smart_truncate(text, limit)
        
        # Should end at a sentence boundary
        self.assertTrue(
            truncated.rstrip().endswith('.') or 
            truncated.rstrip().endswith('...') or
            '[Content truncated' in truncated
        )
    
    def test_preserves_first_and_last_sections(self):
        """Test that smart truncation preserves intro and conclusion."""
        limit = 500
        
        text = """## Introduction
This is the crucial introduction.

## Section 1
Middle content section 1.

## Section 2
Middle content section 2. This has extra content to make it longer.
More content here. And even more content to fill this section.

## Section 3
Middle content section 3.

## Conclusion
This is the important conclusion with CTA."""

        truncated, chars_omitted = self.chunker.smart_truncate(text, limit)
        
        # Should preserve introduction
        self.assertIn("Introduction", truncated)
        self.assertIn("crucial introduction", truncated)
        
        # Should preserve conclusion
        self.assertIn("Conclusion", truncated)
        self.assertIn("important conclusion", truncated)
    
    # =========================================================================
    # Test Case 4: Very long content (multi-chunk)
    # =========================================================================
    def test_very_long_content_multi_chunk(self):
        """Test that very long content (>2x limit) gets split into multiple chunks."""
        limit = 500
        
        # Create content about 3x the limit
        sections = []
        for i in range(10):
            sections.append(f"""## Section {i + 1}

This is section {i + 1} content. It contains important information about topic {i + 1}.

Here is more detail about this section. We discuss various aspects including:
- Point A for section {i + 1}
- Point B for section {i + 1}
- Point C for section {i + 1}

The conclusion of this section summarizes the key points.""")
        
        long_text = "\n\n".join(sections)
        
        result = self.chunker.chunk_content(long_text, max_chars=limit)
        
        # Should have multiple chunks
        self.assertGreater(len(result), 1)
        self.assertFalse(result.single_chunk)
        
        # Each chunk should be within limit (approximately)
        for chunk in result.chunks:
            self.assertLessEqual(len(chunk), limit + 300)  # Allow context header overhead
        
        # Metadata should be consistent
        self.assertEqual(len(result.chunks), len(result.metadata))
        for meta in result.metadata:
            self.assertEqual(meta.total_chunks, len(result))
        
        # Non-first chunks should have context headers
        for i, chunk in enumerate(result.chunks):
            if i > 0:
                self.assertIn("[This is part", chunk)
    
    def test_multi_chunk_preserves_content(self):
        """Test that multi-chunk splitting preserves all content."""
        limit = 500
        
        # Create structured content
        original_sections = ["Section " + str(i) for i in range(5)]
        text = "\n\n".join([f"## {s}\n\nContent for {s}." for s in original_sections])
        
        result = self.chunker.chunk_content(text, max_chars=limit)
        
        # All section headers should appear somewhere in the chunks
        combined = " ".join(result.chunks)
        for section in original_sections:
            self.assertIn(section, combined)
    
    # =========================================================================
    # Test Case 5: Non-English content (Dutch with compound words)
    # =========================================================================
    def test_dutch_content_token_estimation(self):
        """Test token estimation for Dutch (compound words = higher chars/token)."""
        dutch_chunker = ContentChunker(provider="ANTHROPIC", language="nl")
        english_chunker = ContentChunker(provider="ANTHROPIC", language="en")
        
        # Dutch compound words
        dutch_text = "verzekeringspremie overlijdensrisicoverzekering hypotheekrente"
        english_text = "insurance premium life insurance policy mortgage rate"
        
        dutch_tokens = dutch_chunker.estimate_tokens(dutch_text)
        english_tokens = english_chunker.estimate_tokens(english_text)
        
        # Dutch should estimate fewer tokens for same text length
        # because compound words are counted as single tokens
        self.assertEqual(
            dutch_chunker.chars_per_token, 
            LANGUAGE_CHAR_PER_TOKEN["nl"]
        )
        self.assertEqual(
            dutch_chunker.chars_per_token, 
            4.5
        )
        self.assertGreater(dutch_chunker.chars_per_token, english_chunker.chars_per_token)
    
    def test_dutch_content_chunking(self):
        """Test chunking of Dutch content with appropriate limits."""
        dutch_chunker = ContentChunker(provider="ANTHROPIC", language="nl")
        
        # Dutch banking content
        dutch_text = """## Hypotheekverstrekker

Als u een hypotheek afsluit, bent u verplicht een overlijdensrisicoverzekering af te sluiten.

## Verzekeringsvoorwaarden

De verzekeringsmaatschappij bepaalt de premie op basis van uw persoonlijke omstandigheden.

## Belastingvoordeel

U kunt de hypotheekrente aftrekken van uw belastbaar inkomen."""
        
        # Should handle Dutch compound words correctly
        result = dutch_chunker.chunk_content(dutch_text)
        
        self.assertEqual(len(result), 1)
        self.assertTrue(result.single_chunk)
        
        # Token estimation should use Dutch ratio
        tokens = dutch_chunker.estimate_tokens(dutch_text)
        expected_tokens = len(dutch_text) / 4.5  # Dutch chars/token ratio
        self.assertAlmostEqual(tokens, expected_tokens, delta=expected_tokens * 0.2)
    
    def test_romanian_content(self):
        """Test token estimation for Romanian content."""
        ro_chunker = ContentChunker(provider="ANTHROPIC", language="ro")
        
        # Romanian banking content
        ro_text = """## Servicii Bancare

Banca noastră oferă o gamă largă de produse și servicii.

## Credite Ipotecare

Oferim cele mai bune rate pentru creditele ipotecare."""
        
        result = ro_chunker.chunk_content(ro_text)
        
        self.assertEqual(len(result), 1)
        self.assertEqual(ro_chunker.chars_per_token, LANGUAGE_CHAR_PER_TOKEN["ro"])
    
    # =========================================================================
    # Additional test cases
    # =========================================================================
    def test_provider_specific_limits(self):
        """Test that different providers have different limits."""
        anthropic_chunker = ContentChunker(provider="ANTHROPIC")
        openai_chunker = ContentChunker(provider="OPENAI")
        mistral_chunker = ContentChunker(provider="MISTRAL")
        
        # Verify provider limits are loaded
        self.assertEqual(
            anthropic_chunker.limits.safe_content_tokens,
            PROVIDER_LIMITS["ANTHROPIC"].safe_content_tokens
        )
        self.assertEqual(
            openai_chunker.limits.safe_content_tokens,
            PROVIDER_LIMITS["OPENAI"].safe_content_tokens
        )
        self.assertEqual(
            mistral_chunker.limits.safe_content_tokens,
            PROVIDER_LIMITS["MISTRAL"].safe_content_tokens
        )
    
    def test_invalid_provider_raises_error(self):
        """Test that invalid provider raises ValueError."""
        with self.assertRaises(ValueError):
            ContentChunker(provider="INVALID_PROVIDER")
    
    def test_chunk_result_iteration(self):
        """Test that ChunkResult supports iteration and indexing."""
        result = ChunkResult(
            chunks=["chunk1", "chunk2", "chunk3"],
            metadata=[
                ChunkMetadata(0, 3, 100, 30),
                ChunkMetadata(1, 3, 100, 35),
                ChunkMetadata(2, 3, 100, 35),
            ],
            single_chunk=False
        )
        
        # Test len
        self.assertEqual(len(result), 3)
        
        # Test iteration
        chunks_list = list(result)
        self.assertEqual(chunks_list, ["chunk1", "chunk2", "chunk3"])
        
        # Test indexing
        self.assertEqual(result[0], "chunk1")
        self.assertEqual(result[2], "chunk3")
    
    def test_convenience_function_chunk_content(self):
        """Test the convenience function."""
        text = "Short content for testing."
        result = chunk_content(text, provider="ANTHROPIC")
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result.chunks[0], text)
    
    def test_section_header_extraction(self):
        """Test extraction of section headers."""
        text = """## First Header

Content here.

## Second Header

More content.

### Subheader

Even more content."""
        
        headers = self.chunker._extract_section_headers(text)
        
        self.assertIn("First Header", headers)
        self.assertIn("Second Header", headers)
        self.assertIn("Subheader", headers)


class TestAuditResultMerger(unittest.TestCase):
    """Test cases for AuditResultMerger class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.chunk_metadata = [
            ChunkMetadata(0, 2, 1000, 500),
            ChunkMetadata(1, 2, 1000, 500),
        ]
    
    def test_single_result_returns_unchanged(self):
        """Test that single result returns without modification."""
        result = {"seo_audit": {"overall_score": 75}, "issues": [{"id": 1}]}
        
        merged = AuditResultMerger.merge_audit_results(
            [result],
            [self.chunk_metadata[0]],
            "SEO_AUDIT"
        )
        
        self.assertEqual(merged, result)
    
    def test_seo_audit_score_averaging(self):
        """Test weighted averaging of SEO scores."""
        results = [
            {"seo_audit": {"overall_score": 80}, "issues": []},
            {"seo_audit": {"overall_score": 60}, "issues": []},
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            self.chunk_metadata,
            "SEO_AUDIT"
        )
        
        # Equal weights, so average is 70
        self.assertEqual(merged["seo_audit"]["overall_score"], 70)
    
    def test_issue_deduplication(self):
        """Test that duplicate issues are removed."""
        issue1 = {"type": "missing_alt", "element": "img1"}
        issue2 = {"type": "missing_alt", "element": "img2"}
        
        results = [
            {"seo_audit": {"overall_score": 80}, "issues": [issue1, issue2]},
            {"seo_audit": {"overall_score": 70}, "issues": [issue1]},  # Duplicate
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            self.chunk_metadata,
            "SEO_AUDIT"
        )
        
        self.assertEqual(len(merged["issues"]), 2)
    
    def test_geo_audit_merging(self):
        """Test GEO audit result merging."""
        results = [
            {
                "geo_audit": {
                    "ai_citation_likelihood": 80,
                    "authority_score": 70,
                    "quotable_statements_count": 5,
                    "entities_detected": ["ING", "Netherlands"]
                },
                "optimization_opportunities": [
                    {"category": "citations", "priority": "high"}
                ],
                "quotable_excerpts": ["Quote 1"],
                "missing_elements": ["statistics"]
            },
            {
                "geo_audit": {
                    "ai_citation_likelihood": 60,
                    "authority_score": 80,
                    "quotable_statements_count": 3,
                    "entities_detected": ["Amsterdam", "Netherlands"]
                },
                "optimization_opportunities": [
                    {"category": "structure", "priority": "medium"}
                ],
                "quotable_excerpts": ["Quote 2"],
                "missing_elements": ["expert opinions"]
            },
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            self.chunk_metadata,
            "GEO_AUDIT"
        )
        
        # Scores should be averaged
        self.assertEqual(merged["geo_audit"]["ai_citation_likelihood"], 70)
        self.assertEqual(merged["geo_audit"]["authority_score"], 75)
        
        # Quotable statements should be summed
        self.assertEqual(merged["geo_audit"]["quotable_statements_count"], 8)
        
        # Entities should be deduplicated
        entities = merged["geo_audit"]["entities_detected"]
        self.assertIn("ING", entities)
        self.assertIn("Amsterdam", entities)
        self.assertIn("Netherlands", entities)
        self.assertEqual(len(entities), 3)  # No duplicates
    
    def test_violation_audit_merging(self):
        """Test greenwashing/advertisement violation merging."""
        results = [
            {"violations": [{"text": "green energy", "type": "vague_claim"}]},
            {"violations": [{"text": "sustainable", "type": "unsubstantiated"}]},
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            self.chunk_metadata,
            "GREENWASHING"
        )
        
        self.assertEqual(len(merged["violations"]), 2)
    
    def test_generic_audit_merging(self):
        """Test generic merging for unknown audit types."""
        results = [
            {"custom_field": "value1", "items": [1, 2]},
            {"custom_field": "value2", "items": [2, 3]},
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            self.chunk_metadata,
            "UNKNOWN_AUDIT_TYPE"
        )
        
        # Should use first result's scalar values
        self.assertEqual(merged["custom_field"], "value1")
        
        # Should merge list values
        self.assertEqual(len(merged["items"]), 3)  # Deduplicated
    
    def test_weighted_averaging_different_weights(self):
        """Test weighted averaging with unequal chunk sizes."""
        # First chunk is 3x larger
        metadata = [
            ChunkMetadata(0, 2, 1000, 750),
            ChunkMetadata(1, 2, 1000, 250),
        ]
        
        results = [
            {"seo_audit": {"overall_score": 100}},  # Larger chunk
            {"seo_audit": {"overall_score": 0}},    # Smaller chunk
        ]
        
        merged = AuditResultMerger.merge_audit_results(
            results,
            metadata,
            "SEO_AUDIT"
        )
        
        # Should be weighted: (100 * 0.75) + (0 * 0.25) = 75
        self.assertEqual(merged["seo_audit"]["overall_score"], 75)
    
    def test_merge_chunk_results_convenience_function(self):
        """Test the convenience function for merging."""
        results = [
            {"seo_audit": {"overall_score": 80}},
            {"seo_audit": {"overall_score": 60}},
        ]
        
        merged = merge_chunk_results(
            results,
            self.chunk_metadata,
            "SEO_AUDIT"
        )
        
        self.assertEqual(merged["seo_audit"]["overall_score"], 70)
    
    def test_empty_results_handling(self):
        """Test handling of empty results list."""
        merged = AuditResultMerger.merge_audit_results([], [], "SEO_AUDIT")
        self.assertEqual(merged, {})


class TestTokenEstimation(unittest.TestCase):
    """Test cases for token estimation."""
    
    def test_english_token_estimation(self):
        """Test token estimation for English text."""
        chunker = ContentChunker(provider="ANTHROPIC", language="en")
        
        # 1000 characters should be approximately 250 tokens
        text = "a" * 1000
        tokens = chunker.estimate_tokens(text)
        
        self.assertAlmostEqual(tokens, 250, delta=50)
    
    def test_whitespace_adjustment(self):
        """Test that whitespace-heavy text gets adjusted token count."""
        chunker = ContentChunker(provider="ANTHROPIC")
        
        dense_text = "abcdefghijklmnop" * 100
        sparse_text = "a b c d e f g h " * 200
        
        dense_tokens = chunker.estimate_tokens(dense_text)
        sparse_tokens = chunker.estimate_tokens(sparse_text)
        
        # Sparse text should have slightly fewer tokens per char
        # due to whitespace adjustment
        dense_ratio = dense_tokens / len(dense_text)
        sparse_ratio = sparse_tokens / len(sparse_text)
        
        self.assertLessEqual(sparse_ratio, dense_ratio)
    
    def test_char_estimation_from_tokens(self):
        """Test converting token count back to characters."""
        chunker = ContentChunker(provider="ANTHROPIC", language="en")
        
        tokens = 1000
        chars = chunker.estimate_chars_for_tokens(tokens)
        
        # English: 4 chars per token
        self.assertEqual(chars, 4000)
    
    def test_char_estimation_dutch(self):
        """Test character estimation for Dutch."""
        chunker = ContentChunker(provider="ANTHROPIC", language="nl")
        
        tokens = 1000
        chars = chunker.estimate_chars_for_tokens(tokens)
        
        # Dutch: 4.5 chars per token
        self.assertEqual(chars, 4500)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and boundary conditions."""
    
    def test_content_with_no_clear_sections(self):
        """Test content without markdown headers or clear sections."""
        chunker = ContentChunker(provider="ANTHROPIC")
        
        # Plain text without structure
        text = "Lorem ipsum dolor sit amet. " * 100
        
        result = chunker.chunk_content(text, max_chars=500)
        
        self.assertGreater(len(result), 0)
        # Should still produce valid chunks
        for chunk in result.chunks:
            self.assertLessEqual(len(chunk), 800)  # Allow some overhead
    
    def test_single_very_long_paragraph(self):
        """Test handling of a single very long paragraph."""
        chunker = ContentChunker(provider="ANTHROPIC")
        
        # One paragraph that's too long
        text = "This is a very long sentence that keeps going and going. " * 50
        
        result = chunker.chunk_content(text, max_chars=500)
        
        self.assertGreater(len(result), 0)
        # First chunk should be truncated appropriately
        self.assertLessEqual(len(result.chunks[0]), 800)
    
    def test_unicode_content(self):
        """Test handling of unicode characters."""
        chunker = ContentChunker(provider="ANTHROPIC")
        
        unicode_text = """## 日本語のセクション

これは日本語のテストです。

## 中文部分

这是中文测试。

## Ελληνικά

Αυτό είναι ένα τεστ."""
        
        result = chunker.chunk_content(unicode_text)
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result.chunks[0], unicode_text)
    
    def test_content_with_only_whitespace(self):
        """Test handling of whitespace-only content."""
        chunker = ContentChunker(provider="ANTHROPIC")
        
        result = chunker.chunk_content("   \n\n\t   ")
        
        # Should return single chunk with whitespace
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
