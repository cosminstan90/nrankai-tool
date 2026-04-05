"""
Content Chunker for Website LLM Analyzer.

Provides intelligent content chunking that respects content boundaries
and provider-specific token limits. Handles splitting of large pages
into multiple chunks and merging of multi-chunk audit results.

Author: Cosmin (via Claude)
Created: 2026-02-11
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


# ============================================================================
# PROVIDER LIMITS CONFIGURATION
# ============================================================================
@dataclass
class ProviderLimits:
    """Token limits for a specific provider."""
    input_tokens: int
    safe_content_tokens: int  # Recommended content tokens (leaving room for system message + response)
    response_tokens: int = 8192


PROVIDER_LIMITS = {
    "ANTHROPIC": ProviderLimits(
        input_tokens=180000,
        safe_content_tokens=100000,  # ~400K chars, very generous
        response_tokens=8192
    ),
    "OPENAI": ProviderLimits(
        input_tokens=128000,
        safe_content_tokens=80000,  # ~320K chars
        response_tokens=8192
    ),
    "MISTRAL": ProviderLimits(
        input_tokens=128000,
        safe_content_tokens=80000,  # ~320K chars
        response_tokens=8192
    ),
}

# Language-specific token estimation multipliers
# Higher values = more chars per token (compound words, etc.)
LANGUAGE_CHAR_PER_TOKEN = {
    "en": 4.0,    # English
    "nl": 4.5,    # Dutch (compound words)
    "de": 4.5,    # German (compound words)
    "fr": 4.2,    # French
    "es": 4.2,    # Spanish
    "it": 4.2,    # Italian
    "pt": 4.2,    # Portuguese
    "ro": 4.2,    # Romanian
    "pl": 4.3,    # Polish
    "default": 4.0,
}


# ============================================================================
# CHUNK METADATA
# ============================================================================
@dataclass
class ChunkMetadata:
    """Metadata about a content chunk."""
    chunk_index: int
    total_chunks: int
    original_length: int
    chunk_length: int
    section_headers: List[str] = field(default_factory=list)
    is_truncated: bool = False
    truncated_chars: int = 0
    context_header: str = ""


@dataclass 
class ChunkResult:
    """Result from chunking content."""
    chunks: List[str]
    metadata: List[ChunkMetadata]
    single_chunk: bool = True
    
    def __len__(self):
        return len(self.chunks)
    
    def __iter__(self):
        return iter(self.chunks)
    
    def __getitem__(self, index):
        return self.chunks[index]


# ============================================================================
# CONTENT CHUNKER CLASS
# ============================================================================
class ContentChunker:
    """
    Intelligent content chunker that respects content boundaries and provider limits.
    
    Features:
    - Provider-specific token limits
    - Smart truncation at natural boundaries (paragraphs, sections)
    - Multi-chunk support for very long pages
    - Language-aware token estimation
    - Preserves intro and conclusion sections when truncating
    """
    
    # Section header patterns (Markdown and common HTML-derived patterns)
    SECTION_PATTERNS = [
        r'^#{1,6}\s+.+$',      # Markdown headers
        r'^\*\*[^*]+\*\*$',     # Bold text as headers
        r'^[A-Z][A-Z\s]+:$',   # ALL CAPS HEADERS:
        r'^={3,}$',            # === dividers
        r'^-{3,}$',            # --- dividers
    ]
    
    def __init__(
        self,
        provider: str = "ANTHROPIC",
        default_max_tokens: Optional[int] = None,
        language: str = "en"
    ):
        """
        Initialize the content chunker.
        
        Args:
            provider: LLM provider name (ANTHROPIC, OPENAI, MISTRAL)
            default_max_tokens: Override the provider's safe content token limit
            language: Content language for token estimation (default: "en")
        """
        self.provider = provider.upper()
        self.language = language.lower()
        
        # Get provider limits
        if self.provider not in PROVIDER_LIMITS:
            raise ValueError(f"Unknown provider: {provider}. Must be one of {list(PROVIDER_LIMITS.keys())}")
        
        self.limits = PROVIDER_LIMITS[self.provider]
        
        # Override max tokens if specified
        if default_max_tokens is not None:
            self.max_content_tokens = default_max_tokens
        else:
            self.max_content_tokens = self.limits.safe_content_tokens
        
        # Calculate max characters based on language
        self.chars_per_token = LANGUAGE_CHAR_PER_TOKEN.get(
            self.language, 
            LANGUAGE_CHAR_PER_TOKEN["default"]
        )
        self.max_content_chars = int(self.max_content_tokens * self.chars_per_token)
    
    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for text.
        
        Uses character-based estimation adjusted for language.
        No external tokenizer dependency.
        
        Args:
            text: Text to estimate tokens for
            
        Returns:
            Estimated token count
        """
        if not text:
            return 0
        
        # Base estimation: chars / chars_per_token
        base_tokens = len(text) / self.chars_per_token
        
        # Adjust for whitespace (which typically gets merged with adjacent tokens)
        whitespace_ratio = len(re.findall(r'\s+', text)) / max(len(text), 1)
        adjustment = 1.0 - (whitespace_ratio * 0.1)  # Up to 10% reduction for whitespace-heavy text
        
        return int(base_tokens * adjustment)
    
    def estimate_chars_for_tokens(self, tokens: int) -> int:
        """Convert token count to estimated character count."""
        return int(tokens * self.chars_per_token)
    
    def chunk_content(
        self,
        text: str,
        provider: Optional[str] = None,
        max_tokens: Optional[int] = None,
        max_chars: Optional[int] = None
    ) -> ChunkResult:
        """
        Split content into chunks that fit within provider limits.
        
        Strategy (in priority order):
        1. If content fits in one chunk: return as-is (most common case)
        2. If slightly over limit: smart truncate at natural boundary
        3. If significantly over (>2x): split into multiple chunks by sections
        
        Args:
            text: Content to chunk
            provider: Override the instance's provider (optional)
            max_tokens: Override max token limit (optional)
            max_chars: Override max character limit (optional, takes precedence over max_tokens)
            
        Returns:
            ChunkResult with chunks and metadata
        """
        if not text:
            return ChunkResult(
                chunks=[""],
                metadata=[ChunkMetadata(
                    chunk_index=0,
                    total_chunks=1,
                    original_length=0,
                    chunk_length=0
                )],
                single_chunk=True
            )
        
        # Determine effective limits
        if provider:
            effective_provider = provider.upper()
            if effective_provider in PROVIDER_LIMITS:
                limits = PROVIDER_LIMITS[effective_provider]
                effective_max_tokens = max_tokens or limits.safe_content_tokens
            else:
                effective_max_tokens = max_tokens or self.max_content_tokens
        else:
            effective_max_tokens = max_tokens or self.max_content_tokens
        
        # Calculate max characters
        if max_chars is not None:
            effective_max_chars = max_chars
        else:
            effective_max_chars = self.estimate_chars_for_tokens(effective_max_tokens)
        
        original_length = len(text)
        
        # Case 1: Content fits in one chunk
        if original_length <= effective_max_chars:
            return ChunkResult(
                chunks=[text],
                metadata=[ChunkMetadata(
                    chunk_index=0,
                    total_chunks=1,
                    original_length=original_length,
                    chunk_length=original_length,
                    section_headers=self._extract_section_headers(text)
                )],
                single_chunk=True
            )
        
        # Case 2: Slightly over limit (up to 2x) - smart truncate
        if original_length <= effective_max_chars * 2:
            truncated, truncated_chars = self.smart_truncate(text, effective_max_chars)
            return ChunkResult(
                chunks=[truncated],
                metadata=[ChunkMetadata(
                    chunk_index=0,
                    total_chunks=1,
                    original_length=original_length,
                    chunk_length=len(truncated),
                    section_headers=self._extract_section_headers(truncated),
                    is_truncated=True,
                    truncated_chars=truncated_chars
                )],
                single_chunk=True
            )
        
        # Case 3: Significantly over limit - split into multiple chunks
        return self._split_into_chunks(text, effective_max_chars)
    
    def smart_truncate(
        self,
        text: str,
        max_chars: int,
        preserve_conclusion: bool = True
    ) -> Tuple[str, int]:
        """
        Truncate text at a natural boundary.
        
        Rules:
        - Never cut mid-sentence
        - Prefer cutting at section boundaries (## headings, blank lines)
        - Optionally preserve last section (conclusion/CTA)
        - Add truncation note
        
        Args:
            text: Text to truncate
            max_chars: Maximum characters
            preserve_conclusion: Whether to preserve the last section
            
        Returns:
            Tuple of (truncated_text, chars_omitted)
        """
        if len(text) <= max_chars:
            return text, 0
        
        # Extract sections
        sections = self._split_into_sections(text)
        
        if not sections:
            # No clear sections - fall back to paragraph-based truncation
            return self._truncate_at_paragraph(text, max_chars)
        
        # Reserve space for truncation note (~100 chars)
        note_reserve = 100
        available_chars = max_chars - note_reserve
        
        if preserve_conclusion and len(sections) >= 2:
            # Try to keep first and last sections
            first_section = sections[0]
            last_section = sections[-1]
            
            first_len = len(first_section)
            last_len = len(last_section)
            
            if first_len + last_len + note_reserve < available_chars:
                # Can fit both - add middle sections until we hit limit
                middle_sections = sections[1:-1]
                result_sections = [first_section]
                chars_used = first_len
                
                for section in middle_sections:
                    if chars_used + len(section) + last_len + note_reserve <= max_chars:
                        result_sections.append(section)
                        chars_used += len(section)
                    else:
                        break
                
                # Calculate omitted content
                omitted_sections = len(sections) - len(result_sections) - 1
                omitted_chars = sum(len(s) for s in sections[len(result_sections):-1])
                
                if omitted_chars > 0:
                    truncation_note = f"\n\n[Content truncated: {omitted_chars:,} characters ({omitted_sections} sections) omitted from middle]\n\n"
                    result_sections.append(truncation_note)
                
                result_sections.append(last_section)
                return '\n\n'.join(result_sections), omitted_chars
        
        # Fall back: truncate from end, keeping first sections
        result_sections = []
        chars_used = 0
        
        for section in sections:
            if chars_used + len(section) <= available_chars:
                result_sections.append(section)
                chars_used += len(section)
            else:
                # Try to include partial section at sentence boundary
                remaining = available_chars - chars_used
                if remaining > 200:  # Worth including partial
                    partial = self._truncate_at_sentence(section, remaining)
                    if partial:
                        result_sections.append(partial)
                        chars_used += len(partial)
                break
        
        omitted_chars = len(text) - chars_used
        truncation_note = f"\n\n[Content truncated: {omitted_chars:,} characters omitted from end]"
        
        return '\n\n'.join(result_sections) + truncation_note, omitted_chars
    
    def _truncate_at_paragraph(self, text: str, max_chars: int) -> Tuple[str, int]:
        """Truncate at paragraph boundary when no clear sections exist."""
        paragraphs = text.split('\n\n')
        result = []
        chars_used = 0
        note_reserve = 80
        
        for para in paragraphs:
            if chars_used + len(para) + 2 <= max_chars - note_reserve:  # +2 for \n\n
                result.append(para)
                chars_used += len(para) + 2
            else:
                # Try sentence truncation on last paragraph
                remaining = max_chars - chars_used - note_reserve
                if remaining > 100:
                    partial = self._truncate_at_sentence(para, remaining)
                    if partial:
                        result.append(partial)
                        chars_used += len(partial)
                break
        
        omitted = len(text) - chars_used
        final_text = '\n\n'.join(result)
        
        if omitted > 0:
            final_text += f"\n\n[Content truncated: {omitted:,} characters omitted]"
        
        return final_text, omitted
    
    def _truncate_at_sentence(self, text: str, max_chars: int) -> str:
        """Truncate at the last complete sentence within the character limit."""
        if len(text) <= max_chars:
            return text
        
        # Look for sentence boundaries
        truncated = text[:max_chars]
        
        # Find last sentence-ending punctuation
        sentence_ends = ['.', '!', '?', '。', '！', '？']
        last_end = -1
        
        for end_char in sentence_ends:
            pos = truncated.rfind(end_char)
            if pos > last_end:
                last_end = pos
        
        if last_end > max_chars * 0.5:  # At least half the content
            return truncated[:last_end + 1]
        
        # Fall back to word boundary
        last_space = truncated.rfind(' ')
        if last_space > max_chars * 0.7:
            return truncated[:last_space] + '...'
        
        return truncated + '...'
    
    def _split_into_sections(self, text: str) -> List[str]:
        """Split text into logical sections based on headers and blank lines."""
        # Try splitting by markdown headers first
        header_pattern = r'\n(#{1,6}\s+[^\n]+)\n'
        
        # Check if text has markdown headers
        if re.search(header_pattern, '\n' + text + '\n'):
            # Split by headers, keeping the headers
            parts = re.split(r'\n(?=#{1,6}\s+)', text)
            sections = [p.strip() for p in parts if p.strip()]
            if len(sections) > 1:
                return sections
        
        # Try splitting by double newlines (paragraphs)
        paragraphs = text.split('\n\n')
        
        # Group short paragraphs into sections (aim for ~500-1000 char sections)
        sections = []
        current_section = []
        current_length = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            # Check if this paragraph starts with a header-like pattern
            is_header_like = any(
                re.match(pattern, para, re.MULTILINE) 
                for pattern in self.SECTION_PATTERNS
            )
            
            if is_header_like and current_section:
                # Start new section
                sections.append('\n\n'.join(current_section))
                current_section = [para]
                current_length = len(para)
            elif current_length + len(para) > 2000 and current_section:
                # Section getting too long, split here
                sections.append('\n\n'.join(current_section))
                current_section = [para]
                current_length = len(para)
            else:
                current_section.append(para)
                current_length += len(para)
        
        if current_section:
            sections.append('\n\n'.join(current_section))
        
        return sections
    
    def _split_into_chunks(self, text: str, max_chars: int) -> ChunkResult:
        """Split content into multiple chunks for very long pages."""
        sections = self._split_into_sections(text)
        
        if not sections:
            # No clear sections - split by character count with overlap
            return self._split_by_size(text, max_chars)
        
        chunks = []
        chunk_metadata = []
        current_chunk_sections = []
        current_chunk_chars = 0
        previous_sections_summary = []
        
        # Reserve space for context header
        context_reserve = 300
        effective_max = max_chars - context_reserve
        
        for section in sections:
            section_headers = self._extract_section_headers(section)
            
            if current_chunk_chars + len(section) <= effective_max:
                current_chunk_sections.append(section)
                current_chunk_chars += len(section)
            else:
                # Save current chunk
                if current_chunk_sections:
                    chunk_index = len(chunks)
                    chunk_text = '\n\n'.join(current_chunk_sections)
                    
                    # Add context header for non-first chunks
                    context_header = ""
                    if chunk_index > 0:
                        context_header = self._create_context_header(
                            chunk_index + 1,
                            len(sections),  # Estimate total chunks
                            previous_sections_summary
                        )
                        chunk_text = context_header + chunk_text
                    
                    chunks.append(chunk_text)
                    chunk_metadata.append(ChunkMetadata(
                        chunk_index=chunk_index,
                        total_chunks=0,  # Updated later
                        original_length=len(text),
                        chunk_length=len(chunk_text),
                        section_headers=self._extract_section_headers(chunk_text),
                        context_header=context_header
                    ))
                    
                    # Track covered sections
                    for s in current_chunk_sections:
                        headers = self._extract_section_headers(s)
                        if headers:
                            previous_sections_summary.extend(headers[:2])
                
                # Start new chunk
                # Handle sections larger than max_chars
                if len(section) > effective_max:
                    # Split large section
                    truncated, _ = self.smart_truncate(section, effective_max, preserve_conclusion=False)
                    current_chunk_sections = [truncated]
                    current_chunk_chars = len(truncated)
                else:
                    current_chunk_sections = [section]
                    current_chunk_chars = len(section)
        
        # Don't forget the last chunk
        if current_chunk_sections:
            chunk_index = len(chunks)
            chunk_text = '\n\n'.join(current_chunk_sections)
            
            context_header = ""
            if chunk_index > 0:
                context_header = self._create_context_header(
                    chunk_index + 1,
                    chunk_index + 1,
                    previous_sections_summary
                )
                chunk_text = context_header + chunk_text
            
            chunks.append(chunk_text)
            chunk_metadata.append(ChunkMetadata(
                chunk_index=chunk_index,
                total_chunks=0,
                original_length=len(text),
                chunk_length=len(chunk_text),
                section_headers=self._extract_section_headers(chunk_text),
                context_header=context_header
            ))
        
        # Update total_chunks in all metadata
        total = len(chunks)
        for meta in chunk_metadata:
            meta.total_chunks = total
        
        return ChunkResult(
            chunks=chunks,
            metadata=chunk_metadata,
            single_chunk=(total == 1)
        )
    
    def _split_by_size(self, text: str, max_chars: int) -> ChunkResult:
        """Split by size when no clear sections exist."""
        chunks = []
        chunk_metadata = []
        
        # Use paragraph-aware splitting
        paragraphs = text.split('\n\n')
        current_chunk = []
        current_chars = 0
        context_reserve = 300
        effective_max = max_chars - context_reserve
        
        for para in paragraphs:
            if current_chars + len(para) <= effective_max:
                current_chunk.append(para)
                current_chars += len(para) + 2
            else:
                if current_chunk:
                    chunk_index = len(chunks)
                    chunk_text = '\n\n'.join(current_chunk)
                    
                    context_header = ""
                    if chunk_index > 0:
                        context_header = f"[Continued from previous chunk (part {chunk_index + 1})]\n\n"
                        chunk_text = context_header + chunk_text
                    
                    chunks.append(chunk_text)
                    chunk_metadata.append(ChunkMetadata(
                        chunk_index=chunk_index,
                        total_chunks=0,
                        original_length=len(text),
                        chunk_length=len(chunk_text),
                        context_header=context_header
                    ))
                
                current_chunk = [para]
                current_chars = len(para)
        
        # Last chunk
        if current_chunk:
            chunk_index = len(chunks)
            chunk_text = '\n\n'.join(current_chunk)
            
            context_header = ""
            if chunk_index > 0:
                context_header = f"[Continued from previous chunk (part {chunk_index + 1})]\n\n"
                chunk_text = context_header + chunk_text
            
            chunks.append(chunk_text)
            chunk_metadata.append(ChunkMetadata(
                chunk_index=chunk_index,
                total_chunks=0,
                original_length=len(text),
                chunk_length=len(chunk_text),
                context_header=context_header
            ))
        
        # Update totals
        total = len(chunks)
        for meta in chunk_metadata:
            meta.total_chunks = total
        
        return ChunkResult(
            chunks=chunks,
            metadata=chunk_metadata,
            single_chunk=(total == 1)
        )
    
    def _extract_section_headers(self, text: str) -> List[str]:
        """Extract section headers from text."""
        headers = []
        
        # Markdown headers
        md_headers = re.findall(r'^#{1,6}\s+(.+)$', text, re.MULTILINE)
        headers.extend(md_headers)
        
        # Bold text headers (common in converted HTML)
        bold_headers = re.findall(r'^\*\*([^*]+)\*\*$', text, re.MULTILINE)
        headers.extend(bold_headers)
        
        return headers[:10]  # Limit to first 10
    
    def _create_context_header(
        self,
        chunk_num: int,
        total_chunks: int,
        previous_sections: List[str]
    ) -> str:
        """Create a context header for non-first chunks."""
        header_parts = [
            f"[This is part {chunk_num}/{total_chunks} of the page analysis]"
        ]
        
        if previous_sections:
            # Limit to last 5 sections
            recent = previous_sections[-5:]
            section_list = ", ".join(f'"{s}"' for s in recent)
            header_parts.append(f"Previous sections covered: {section_list}")
        
        return '\n'.join(header_parts) + '\n\n'


# ============================================================================
# AUDIT RESULT MERGER
# ============================================================================
class AuditResultMerger:
    """
    Merges audit results from multiple chunks into a single coherent result.
    
    Handles:
    - Score averaging (weighted by content length)
    - Issue/violation deduplication
    - Quick win consolidation
    """
    
    @staticmethod
    def merge_audit_results(
        results: List[Dict[str, Any]],
        chunk_metadata: List[ChunkMetadata],
        audit_type: str
    ) -> Dict[str, Any]:
        """
        Merge multiple chunk audit results into a single result.
        
        Args:
            results: List of audit result dictionaries from each chunk
            chunk_metadata: Metadata about each chunk (for weighting)
            audit_type: Type of audit (SEO_AUDIT, GEO_AUDIT, etc.)
            
        Returns:
            Merged audit result dictionary
        """
        if not results:
            return {}
        
        if len(results) == 1:
            return results[0]
        
        # Calculate weights based on content length
        total_length = sum(m.chunk_length for m in chunk_metadata)
        weights = [m.chunk_length / total_length for m in chunk_metadata]
        
        audit_type = audit_type.upper()
        
        # Route to appropriate merger based on audit type
        merger_map = {
            "SEO_AUDIT": AuditResultMerger._merge_seo_audit,
            "GEO_AUDIT": AuditResultMerger._merge_geo_audit,
            "ACCESSIBILITY_AUDIT": AuditResultMerger._merge_accessibility_audit,
            "CONTENT_QUALITY": AuditResultMerger._merge_content_quality,
            "UX_CONTENT": AuditResultMerger._merge_ux_audit,
            "LEGAL_GDPR": AuditResultMerger._merge_legal_audit,
            "GREENWASHING": AuditResultMerger._merge_violation_audit,
            "ADVERTISMENT": AuditResultMerger._merge_violation_audit,
            "SPELLING_GRAMMAR": AuditResultMerger._merge_spelling_audit,
        }
        
        merger_func = merger_map.get(audit_type, AuditResultMerger._merge_generic)
        return merger_func(results, weights)
    
    @staticmethod
    def _weighted_average(values: List[Union[int, float]], weights: List[float]) -> float:
        """Calculate weighted average of values."""
        if not values or not weights:
            return 0
        
        weighted_sum = sum(v * w for v, w in zip(values, weights))
        return weighted_sum
    
    @staticmethod
    def _deduplicate_list(items: List[Any], key_func=None) -> List[Any]:
        """Deduplicate a list while preserving order."""
        if key_func is None:
            key_func = lambda x: json.dumps(x, sort_keys=True) if isinstance(x, dict) else str(x)
        
        seen = set()
        result = []
        for item in items:
            key = key_func(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result
    
    @staticmethod
    def _merge_seo_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge SEO audit results."""
        merged = {"seo_audit": {}, "issues": [], "quick_wins": []}
        
        # Collect scores
        scores = []
        for r in results:
            audit = r.get("seo_audit", {})
            if "overall_score" in audit:
                scores.append(audit["overall_score"])
        
        if scores:
            merged["seo_audit"]["overall_score"] = round(AuditResultMerger._weighted_average(scores, weights[:len(scores)]))
        
        # Merge issues
        all_issues = []
        for r in results:
            all_issues.extend(r.get("issues", []))
        merged["issues"] = AuditResultMerger._deduplicate_list(all_issues)
        
        # Merge quick wins
        all_wins = []
        for r in results:
            all_wins.extend(r.get("quick_wins", []))
        merged["quick_wins"] = AuditResultMerger._deduplicate_list(all_wins)
        
        # Copy other fields from first result
        for key, value in results[0].items():
            if key not in merged:
                merged[key] = value
        
        return merged
    
    @staticmethod
    def _merge_geo_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge GEO audit results."""
        merged = {
            "geo_audit": {},
            "optimization_opportunities": [],
            "quotable_excerpts": [],
            "missing_elements": []
        }
        
        # Collect and average scores
        score_fields = ["overall_score", "citation_probability", "authority_score", "structure_score", "factual_density_score"]
        for field in score_fields:
            values = []
            for r in results:
                audit = r.get("geo_audit", {})
                if field in audit:
                    values.append(audit[field])
            if values:
                merged["geo_audit"][field] = round(AuditResultMerger._weighted_average(values, weights[:len(values)]))
        
        # Sum quotable statements
        total_quotable = sum(
            r.get("geo_audit", {}).get("quotable_statements_count", 0) 
            for r in results
        )
        merged["geo_audit"]["quotable_statements_count"] = total_quotable
        
        # Merge entities (deduplicated)
        all_entities = []
        for r in results:
            all_entities.extend(r.get("geo_audit", {}).get("entities_detected", []))
        merged["geo_audit"]["entities_detected"] = list(set(all_entities))
        
        # Content type from first chunk (usually most representative)
        if results:
            merged["geo_audit"]["content_type"] = results[0].get("geo_audit", {}).get("content_type", "mixed")
        
        # Merge lists
        for r in results:
            merged["optimization_opportunities"].extend(r.get("optimization_opportunities", []))
            merged["quotable_excerpts"].extend(r.get("quotable_excerpts", []))
            merged["missing_elements"].extend(r.get("missing_elements", []))
        
        # Deduplicate
        merged["optimization_opportunities"] = AuditResultMerger._deduplicate_list(
            merged["optimization_opportunities"]
        )[:10]  # Limit to top 10
        merged["quotable_excerpts"] = list(set(merged["quotable_excerpts"]))[:10]
        merged["missing_elements"] = list(set(merged["missing_elements"]))
        
        # Copy remaining fields from first result
        for key in ["competitor_differentiation"]:
            if key not in merged and results:
                merged[key] = results[0].get(key, "")
        
        return merged
    
    @staticmethod
    def _merge_accessibility_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge accessibility audit results."""
        merged = {"accessibility_audit": {}, "issues": [], "recommendations": []}
        
        scores = []
        for r in results:
            audit = r.get("accessibility_audit", {})
            if "overall_score" in audit:
                scores.append(audit["overall_score"])
        
        if scores:
            merged["accessibility_audit"]["overall_score"] = round(
                AuditResultMerger._weighted_average(scores, weights[:len(scores)])
            )
        
        # WCAG level - use the lowest (most conservative)
        levels = []
        level_order = {"AAA": 3, "AA": 2, "A": 1, "unknown": 0}
        for r in results:
            level = r.get("accessibility_audit", {}).get("wcag_level", "unknown")
            levels.append(level)
        
        if levels:
            merged["accessibility_audit"]["wcag_level"] = min(
                levels, 
                key=lambda x: level_order.get(x, 0)
            )
        
        # Merge issues
        all_issues = []
        for r in results:
            all_issues.extend(r.get("issues", []))
        merged["issues"] = AuditResultMerger._deduplicate_list(all_issues)
        
        return merged
    
    @staticmethod
    def _merge_content_quality(results: List[Dict], weights: List[float]) -> Dict:
        """Merge content quality audit results."""
        merged = {"quality_audit": {}, "issues": [], "recommendations": []}
        
        scores = []
        for r in results:
            audit = r.get("quality_audit", {})
            if "overall_quality_score" in audit:
                scores.append(audit["overall_quality_score"])
        
        if scores:
            merged["quality_audit"]["overall_quality_score"] = round(
                AuditResultMerger._weighted_average(scores, weights[:len(scores)])
            )
        
        # Content classification - use most common
        classifications = []
        for r in results:
            cls = r.get("quality_audit", {}).get("content_classification")
            if cls:
                classifications.append(cls)
        
        if classifications:
            merged["quality_audit"]["content_classification"] = max(
                set(classifications), 
                key=classifications.count
            )
        
        return merged
    
    @staticmethod
    def _merge_ux_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge UX content audit results."""
        merged = {"ux_audit": {}, "issues": [], "recommendations": []}
        
        scores = []
        for r in results:
            audit = r.get("ux_audit", {})
            if "clarity_score" in audit:
                scores.append(audit["clarity_score"])
        
        if scores:
            merged["ux_audit"]["clarity_score"] = round(
                AuditResultMerger._weighted_average(scores, weights[:len(scores)])
            )
        
        # Conversion potential
        potentials = []
        for r in results:
            pot = r.get("ux_audit", {}).get("conversion_potential")
            if pot:
                potentials.append(pot)
        
        if potentials:
            merged["ux_audit"]["conversion_potential"] = potentials[0]
        
        return merged
    
    @staticmethod
    def _merge_legal_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge legal/GDPR audit results."""
        merged = {"legal_audit": {}, "violations": [], "recommendations": []}
        
        scores = []
        for r in results:
            audit = r.get("legal_audit", {})
            if "gdpr_compliance_score" in audit:
                scores.append(audit["gdpr_compliance_score"])
        
        if scores:
            merged["legal_audit"]["gdpr_compliance_score"] = round(
                AuditResultMerger._weighted_average(scores, weights[:len(scores)])
            )
        
        # Risk level - use highest (most conservative)
        risk_order = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
        risks = []
        for r in results:
            risk = r.get("legal_audit", {}).get("risk_level", "unknown")
            risks.append(risk)
        
        if risks:
            merged["legal_audit"]["risk_level"] = max(
                risks, 
                key=lambda x: risk_order.get(x, 0)
            )
        
        # Merge violations
        all_violations = []
        for r in results:
            all_violations.extend(r.get("violations", []))
        merged["violations"] = AuditResultMerger._deduplicate_list(all_violations)
        
        return merged
    
    @staticmethod
    def _merge_violation_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge greenwashing/advertisement violation audits."""
        merged = {"violations": []}
        
        all_violations = []
        for r in results:
            all_violations.extend(r.get("violations", []))
        
        merged["violations"] = AuditResultMerger._deduplicate_list(all_violations)
        
        return merged
    
    @staticmethod
    def _merge_spelling_audit(results: List[Dict], weights: List[float]) -> Dict:
        """Merge spelling/grammar audit results."""
        merged = {"audit_results": []}
        
        all_results = []
        for r in results:
            all_results.extend(r.get("audit_results", []))
        
        merged["audit_results"] = AuditResultMerger._deduplicate_list(all_results)
        
        return merged
    
    @staticmethod
    def _merge_generic(results: List[Dict], weights: List[float]) -> Dict:
        """Generic merger for unknown audit types."""
        if not results:
            return {}
        
        # Start with first result
        merged = dict(results[0])
        
        # Merge any list fields
        for key, value in merged.items():
            if isinstance(value, list):
                all_items = []
                for r in results:
                    if key in r and isinstance(r[key], list):
                        all_items.extend(r[key])
                merged[key] = AuditResultMerger._deduplicate_list(all_items)
        
        return merged


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================
def chunk_content(
    text: str,
    provider: str = "ANTHROPIC",
    max_chars: Optional[int] = None
) -> ChunkResult:
    """
    Convenience function to chunk content.
    
    Args:
        text: Content to chunk
        provider: LLM provider name
        max_chars: Optional character limit override
        
    Returns:
        ChunkResult with chunks and metadata
    """
    chunker = ContentChunker(provider=provider)
    return chunker.chunk_content(text, max_chars=max_chars)


def merge_chunk_results(
    results: List[Dict[str, Any]],
    chunk_metadata: List[ChunkMetadata],
    audit_type: str
) -> Dict[str, Any]:
    """
    Convenience function to merge chunk results.
    
    Args:
        results: List of audit results from each chunk
        chunk_metadata: Chunk metadata list
        audit_type: Type of audit
        
    Returns:
        Merged audit result
    """
    return AuditResultMerger.merge_audit_results(results, chunk_metadata, audit_type)


# ============================================================================
# MODULE EXPORTS
# ============================================================================
__all__ = [
    # Main classes
    "ContentChunker",
    "AuditResultMerger",
    
    # Data classes
    "ChunkResult",
    "ChunkMetadata",
    "ProviderLimits",
    
    # Constants
    "PROVIDER_LIMITS",
    "LANGUAGE_CHAR_PER_TOKEN",
    
    # Convenience functions
    "chunk_content",
    "merge_chunk_results",
]
