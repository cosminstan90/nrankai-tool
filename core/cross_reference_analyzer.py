"""
Cross-Reference Analyzer for Website LLM Analyzer.

Performs site-wide pattern analysis across all individual page audits.
Detects issues like keyword cannibalization, content gaps, inconsistencies,
and provides strategic recommendations.

Runs as a second-pass analysis AFTER all individual page audits complete.

Author: Cosmin
Created: 2026-02-12
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional, Tuple

# Async LLM clients
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mistralai import Mistral

# Local imports
from logger import get_logger, setup_logging

logger = get_logger(__name__)


# ============================================================================
# DATA CLASSES
# ============================================================================
@dataclass
class PageSummary:
    """Compressed summary of a single page audit for cross-reference."""
    filename: str
    score: Optional[int] = None
    primary_keyword: Optional[str] = None
    secondary_keywords: List[str] = field(default_factory=list)
    top_issues: List[str] = field(default_factory=list)
    content_type: Optional[str] = None
    word_count: Optional[int] = None
    h1_text: Optional[str] = None
    # Additional fields per audit type
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CrossRefStats:
    """Statistics computed from page audits."""
    pages_analyzed: int = 0
    average_score: float = 0.0
    median_score: float = 0.0
    std_deviation: float = 0.0
    min_score: int = 0
    max_score: int = 0
    score_distribution: Dict[str, int] = field(default_factory=dict)
    top_5_pages: List[Dict[str, Any]] = field(default_factory=list)
    bottom_5_pages: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RuleBasedFindings:
    """Findings from rule-based (non-LLM) analysis."""
    keyword_cannibalization: List[Dict[str, Any]] = field(default_factory=list)
    duplicate_h1s: List[Dict[str, Any]] = field(default_factory=list)
    repeated_errors: List[Dict[str, Any]] = field(default_factory=list)
    thin_content_pages: List[str] = field(default_factory=list)
    style_inconsistencies: List[Dict[str, Any]] = field(default_factory=list)
    score_outliers: List[Dict[str, Any]] = field(default_factory=list)
    terminology_variations: List[Dict[str, Any]] = field(default_factory=list)


# ============================================================================
# AUDIT-SPECIFIC EXTRACTORS
# ============================================================================
class AuditExtractor:
    """Extract relevant data from different audit types."""
    
    @staticmethod
    def extract_seo_audit(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from SEO_AUDIT results."""
        seo = data.get('seo_audit', {})
        issues = data.get('issues', [])
        
        return PageSummary(
            filename=filename,
            score=seo.get('overall_score'),
            primary_keyword=seo.get('primary_keyword_detected'),
            secondary_keywords=seo.get('secondary_keywords', []),
            top_issues=[i.get('issue', '') for i in issues[:3]],
            content_type=seo.get('search_intent'),
            word_count=seo.get('word_count'),
            h1_text=seo.get('h1_text'),
            extra={
                'content_score': seo.get('content_score'),
                'technical_score': seo.get('technical_score'),
                'readability_level': seo.get('readability_level'),
                'content_gaps': data.get('content_gaps', []),
                'quick_wins': data.get('quick_wins', [])
            }
        )
    
    @staticmethod
    def extract_geo_audit(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from GEO_AUDIT results."""
        geo = data.get('geo_audit', {})
        
        # Score: use overall_score (v2.0 schema), fallback to ai_citation_likelihood (legacy)
        score = geo.get('overall_score') or geo.get('ai_citation_likelihood')
        if isinstance(score, str):
            try:
                score = int(score)
            except ValueError:
                score = None

        return PageSummary(
            filename=filename,
            score=score,
            primary_keyword=geo.get('primary_topic'),
            top_issues=[geo.get('main_weakness', '')] if geo.get('main_weakness') else [],
            content_type=geo.get('content_type') or geo.get('content_category'),
            extra={
                'citation_probability': geo.get('citation_probability'),
                'factual_density': geo.get('factual_density_score') or geo.get('factual_density'),
                'authority_score': geo.get('authority_score'),
                'entities': geo.get('entities_detected', []) or geo.get('key_entities', []),
                'quotable_statements': geo.get('quotable_statements', []),
            }
        )
    
    @staticmethod
    def extract_brand_voice(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from BRAND_VOICE results."""
        brand = data.get('brand_voice_audit', {})
        
        return PageSummary(
            filename=filename,
            score=brand.get('overall_score') or brand.get('consistency_score'),
            top_issues=brand.get('deviations', [])[:3],
            extra={
                'tone': brand.get('detected_tone'),
                'formality_level': brand.get('formality_level'),
                'terminology': brand.get('key_terminology', []),
                'voice_characteristics': brand.get('voice_characteristics', [])
            }
        )
    
    @staticmethod
    def extract_spelling_grammar(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from SPELLING_GRAMMAR results."""
        sg = data.get('spelling_grammar_audit', {})
        errors = sg.get('errors', [])

        # Categorize errors
        spelling_errors = [r for r in errors if r.get('type') == 'spelling']
        grammar_errors  = [r for r in errors if r.get('type') == 'grammar']

        score = sg.get('overall_score')
        if score is None:
            score = max(0, 100 - len(errors))  # Inverse: fewer errors = higher score

        return PageSummary(
            filename=filename,
            score=score,
            top_issues=[r.get('text', r.get('issue', '')) for r in errors[:3]],
            extra={
                'error_count': len(errors),
                'spelling_errors': spelling_errors,
                'grammar_errors': grammar_errors,
                'style_notes': sg.get('style_notes', [])
            }
        )
    
    @staticmethod
    def extract_greenwashing(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from GREENWASHING results."""
        violations = data.get('violations', [])
        
        return PageSummary(
            filename=filename,
            score=100 - (len(violations) * 10),  # Penalty per violation
            top_issues=[v.get('claim', '') for v in violations[:3]],
            extra={
                'violation_count': len(violations),
                'violations': violations,
                'risk_level': data.get('overall_risk_level'),
                'environmental_claims': data.get('environmental_claims', [])
            }
        )
    
    @staticmethod
    def extract_advertisment(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from ADVERTISMENT results."""
        violations = data.get('violations', [])
        
        return PageSummary(
            filename=filename,
            score=100 - (len(violations) * 10),
            top_issues=[v.get('issue', '') for v in violations[:3]],
            extra={
                'violation_count': len(violations),
                'violations': violations,
                'compliance_status': data.get('compliance_status'),
                'claims': data.get('advertising_claims', [])
            }
        )
    
    @staticmethod
    def extract_content_quality(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from CONTENT_QUALITY results."""
        cq = data.get('content_quality', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=cq.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'eeat_score': cq.get('eeat_score'),
                'depth_score': cq.get('depth_score'),
                'originality_score': cq.get('originality_score'),
                'classification': cq.get('classification'),
                'quick_wins': data.get('quick_wins', []),
            }
        )

    @staticmethod
    def extract_technical_seo(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from TECHNICAL_SEO results."""
        tech = data.get('technical_seo_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=tech.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'schema_types': tech.get('schema_types_detected', []),
                'has_video_schema': tech.get('has_video_schema'),
                'has_video_transcript': tech.get('has_video_transcript'),
                'meta_elements_score': tech.get('meta_elements_score'),
                'structured_data_score': tech.get('structured_data_score'),
                'rich_result_eligible': tech.get('rich_result_eligible', []),
                'requires_verification': data.get('requires_technical_verification', []),
            }
        )

    @staticmethod
    def extract_ux_content(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from UX_CONTENT results."""
        ux = data.get('ux_content_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=ux.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'cta_score': ux.get('cta_score'),
                'navigation_score': ux.get('navigation_score'),
                'trust_score': ux.get('trust_score'),
                'classification': ux.get('classification'),
            }
        )

    @staticmethod
    def extract_accessibility_audit(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from ACCESSIBILITY_AUDIT results."""
        acc = data.get('accessibility_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=acc.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'wcag_level': acc.get('wcag_level'),
                'critical_violations': acc.get('critical_violations', []),
                'alt_text_score': acc.get('alt_text_score'),
                'keyboard_nav_score': acc.get('keyboard_nav_score'),
                'classification': acc.get('classification'),
            }
        )

    @staticmethod
    def extract_legal_gdpr(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from LEGAL_GDPR results."""
        gdpr = data.get('gdpr_audit', {})
        violations = data.get('violations', [])
        return PageSummary(
            filename=filename,
            score=gdpr.get('overall_score'),
            top_issues=[v.get('finding', v.get('issue', '')) for v in violations[:3]],
            extra={
                'has_privacy_policy': gdpr.get('has_privacy_policy'),
                'has_cookie_consent': gdpr.get('has_cookie_consent'),
                'consent_mechanism': gdpr.get('consent_mechanism'),
                'blocker_count': len([v for v in violations if v.get('severity') == 'blocker']),
                'classification': gdpr.get('classification'),
            }
        )

    @staticmethod
    def extract_internal_linking(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from INTERNAL_LINKING results."""
        il = data.get('internal_linking', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=il.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'body_links_count': il.get('body_content_links_count', 0),
                'nav_footer_links': il.get('navigation_footer_links_count', 0),
                'external_links': il.get('external_links_count', 0),
                'generic_anchors': il.get('generic_anchors_found', []),
                'anchor_quality_score': il.get('anchor_quality_score'),
                'cluster_coverage_score': il.get('cluster_coverage_score'),
                'outbound_link_score': il.get('outbound_link_score'),
                'cluster_gaps': data.get('topic_cluster_gaps', []),
            }
        )

    @staticmethod
    def extract_readability_audit(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from READABILITY_AUDIT results."""
        ra = data.get('readability_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=ra.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'flesch_score': ra.get('flesch_kincaid_score') or ra.get('flesch_score'),
                'grade_level': ra.get('grade_level'),
                'avg_sentence_length': ra.get('avg_sentence_length'),
                'passive_voice_pct': ra.get('passive_voice_percentage'),
                'classification': ra.get('classification'),
            }
        )

    @staticmethod
    def extract_competitor_analysis(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from COMPETITOR_ANALYSIS results."""
        cp = data.get('competitive_positioning_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=cp.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'value_proposition_score': cp.get('value_proposition_score'),
                'differentiation_score': cp.get('differentiation_score'),
                'messaging_score': cp.get('messaging_score'),
                'topical_gap_score': cp.get('topical_gap_score'),
                'generic_claims': cp.get('generic_claims_found', []),
                'topical_gaps': data.get('topical_gaps', []),
                'positioning_opportunities': data.get('positioning_opportunities', []),
            }
        )

    @staticmethod
    def extract_content_freshness(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from CONTENT_FRESHNESS results."""
        fa = data.get('freshness_audit', {})
        indicators = data.get('obsolescence_indicators', [])
        return PageSummary(
            filename=filename,
            score=fa.get('overall_score'),
            top_issues=[i.get('finding', i.get('description', '')) for i in indicators[:3]],
            extra={
                'last_updated': fa.get('last_updated_detected'),
                'freshness_classification': fa.get('classification'),
                'update_urgency': fa.get('update_urgency'),
                'stale_sections': fa.get('stale_sections', []),
            }
        )

    @staticmethod
    def extract_ai_overview_optimization(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from AI_OVERVIEW_OPTIMIZATION results."""
        aio = data.get('ai_overview_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=aio.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'featured_snippet_score': aio.get('featured_snippet_score'),
                'structured_answer_score': aio.get('structured_answer_score'),
                'has_faq_schema': aio.get('has_faq_schema'),
                'has_howto_schema': aio.get('has_howto_schema'),
                'classification': aio.get('classification'),
            }
        )

    @staticmethod
    def extract_translation_quality(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from TRANSLATION_QUALITY results."""
        ta = data.get('translation_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=ta.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'source_language': ta.get('source_language'),
                'target_language': ta.get('target_language'),
                'fluency_score': ta.get('fluency_score'),
                'accuracy_score': ta.get('accuracy_score'),
                'classification': ta.get('classification'),
            }
        )

    @staticmethod
    def extract_local_seo(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from LOCAL_SEO results."""
        ls = data.get('local_seo_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=ls.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'nap_consistency': ls.get('nap_consistency_score'),
                'has_local_schema': ls.get('has_local_business_schema'),
                'gbp_signals': ls.get('gbp_optimization_score'),
                'location_signals': ls.get('location_signals', []),
                'classification': ls.get('classification'),
            }
        )

    @staticmethod
    def extract_security_content_audit(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from SECURITY_CONTENT_AUDIT results."""
        sec = data.get('security_content_audit', {})
        findings = data.get('findings', [])
        return PageSummary(
            filename=filename,
            score=sec.get('overall_score'),
            top_issues=[f.get('finding', f.get('issue', '')) for f in findings[:3]],
            extra={
                'exposed_info_count': len([f for f in findings if f.get('risk') == 'high']),
                'https_signals': sec.get('https_signals'),
                'classification': sec.get('classification'),
                'high_risk_findings': [f for f in findings if f.get('risk') == 'high'],
            }
        )

    @staticmethod
    def extract_e_commerce(data: Dict[str, Any], filename: str) -> PageSummary:
        """Extract summary from E_COMMERCE results."""
        ec = data.get('ecommerce_audit', {})
        issues = data.get('issues', [])
        return PageSummary(
            filename=filename,
            score=ec.get('overall_score'),
            top_issues=[i.get('finding', i.get('issue', '')) for i in issues[:3]],
            extra={
                'conversion_score': ec.get('conversion_optimization_score'),
                'trust_score': ec.get('trust_signals_score'),
                'product_content_score': ec.get('product_content_score'),
                'conversion_barriers': data.get('conversion_barriers', []),
                'classification': ec.get('classification'),
            }
        )

    @staticmethod
    def extract_generic(data: Dict[str, Any], filename: str, audit_key: str) -> PageSummary:
        """Generic extractor for audit types without specific handling."""
        # Try to find score in common locations
        score = None
        for key in [audit_key, 'audit', 'analysis', 'results']:
            if key in data and isinstance(data[key], dict):
                for score_key in ['overall_score', 'score', 'total_score']:
                    if score_key in data[key]:
                        score = data[key][score_key]
                        break
        
        # Collect issues from common locations
        issues = []
        for key in ['issues', 'problems', 'violations', 'findings']:
            if key in data and isinstance(data[key], list):
                for item in data[key][:3]:
                    if isinstance(item, dict):
                        issues.append(item.get('issue', item.get('description', str(item))))
                    elif isinstance(item, str):
                        issues.append(item)
        
        return PageSummary(
            filename=filename,
            score=score,
            top_issues=issues[:3],
            extra=data
        )


def get_extractor(audit_type: str):
    """Get the appropriate extractor function for an audit type."""
    extractors = {
        'SEO_AUDIT':                  AuditExtractor.extract_seo_audit,
        'GEO_AUDIT':                  AuditExtractor.extract_geo_audit,
        'BRAND_VOICE':                AuditExtractor.extract_brand_voice,
        'SPELLING_GRAMMAR':           AuditExtractor.extract_spelling_grammar,
        'GREENWASHING':               AuditExtractor.extract_greenwashing,
        'ADVERTISMENT':               AuditExtractor.extract_advertisment,
        'CONTENT_QUALITY':            AuditExtractor.extract_content_quality,
        'TECHNICAL_SEO':              AuditExtractor.extract_technical_seo,
        'UX_CONTENT':                 AuditExtractor.extract_ux_content,
        'ACCESSIBILITY_AUDIT':        AuditExtractor.extract_accessibility_audit,
        'LEGAL_GDPR':                 AuditExtractor.extract_legal_gdpr,
        'INTERNAL_LINKING':           AuditExtractor.extract_internal_linking,
        'READABILITY_AUDIT':          AuditExtractor.extract_readability_audit,
        'COMPETITOR_ANALYSIS':        AuditExtractor.extract_competitor_analysis,
        'CONTENT_FRESHNESS':          AuditExtractor.extract_content_freshness,
        'AI_OVERVIEW_OPTIMIZATION':   AuditExtractor.extract_ai_overview_optimization,
        'TRANSLATION_QUALITY':        AuditExtractor.extract_translation_quality,
        'LOCAL_SEO':                  AuditExtractor.extract_local_seo,
        'SECURITY_CONTENT_AUDIT':     AuditExtractor.extract_security_content_audit,
        'E_COMMERCE':                 AuditExtractor.extract_e_commerce,
    }
    return extractors.get(audit_type.upper())


# ============================================================================
# RULE-BASED ANALYSIS (No LLM required)
# ============================================================================
class RuleBasedAnalyzer:
    """
    Performs rule-based cross-reference analysis without LLM.
    
    Fast and free - useful for quick checks and pattern detection.
    """
    
    def __init__(self, summaries: List[PageSummary], audit_type: str):
        self.summaries = summaries
        self.audit_type = audit_type.upper()
        self.findings = RuleBasedFindings()
    
    def analyze_all(self) -> RuleBasedFindings:
        """Run all applicable rule-based analyses."""
        logger.info("Running rule-based analysis...")
        
        # Common analyses for all audit types
        self._detect_score_outliers()
        
        # Audit-specific analyses
        if self.audit_type == 'SEO_AUDIT':
            self._detect_keyword_cannibalization()
            self._detect_duplicate_h1s()
            self._detect_thin_content()
        
        elif self.audit_type == 'GEO_AUDIT':
            self._detect_entity_inconsistencies()
            
        elif self.audit_type == 'BRAND_VOICE':
            self._detect_tone_drift()
            self._detect_terminology_inconsistencies()
        
        elif self.audit_type == 'SPELLING_GRAMMAR':
            self._detect_repeated_errors()
            self._detect_style_inconsistencies()
        
        elif self.audit_type in ('GREENWASHING', 'ADVERTISMENT'):
            self._detect_claim_contradictions()

        elif self.audit_type == 'TECHNICAL_SEO':
            self._detect_missing_schema_patterns()
            self._detect_missing_video_schema()

        elif self.audit_type == 'INTERNAL_LINKING':
            self._detect_orphan_pages()
            self._detect_anchor_text_homogeneity()

        elif self.audit_type == 'CONTENT_QUALITY':
            self._detect_ai_content_clusters()

        elif self.audit_type == 'ACCESSIBILITY_AUDIT':
            self._detect_repeated_accessibility_violations()

        elif self.audit_type == 'LOCAL_SEO':
            self._detect_nap_inconsistencies()

        elif self.audit_type in (
            'UX_CONTENT', 'LEGAL_GDPR', 'READABILITY_AUDIT',
            'COMPETITOR_ANALYSIS', 'CONTENT_FRESHNESS',
            'AI_OVERVIEW_OPTIMIZATION', 'TRANSLATION_QUALITY',
            'SECURITY_CONTENT_AUDIT', 'E_COMMERCE',
        ):
            self._detect_repeated_issues_generic()

        return self.findings
    
    def _detect_keyword_cannibalization(self):
        """Find pages targeting the same primary keyword."""
        keyword_pages = defaultdict(list)
        
        for summary in self.summaries:
            if summary.primary_keyword:
                # Normalize keyword (lowercase, strip)
                kw = summary.primary_keyword.lower().strip()
                if kw:
                    keyword_pages[kw].append(summary.filename)
        
        # Find keywords with multiple pages
        for keyword, pages in keyword_pages.items():
            if len(pages) > 1:
                self.findings.keyword_cannibalization.append({
                    'keyword': keyword,
                    'affected_pages': pages,
                    'page_count': len(pages),
                    'severity': 'high' if len(pages) > 3 else 'medium',
                    'recommendation': f"Consolidate {len(pages)} pages targeting '{keyword}' or differentiate their focus"
                })
        
        # Sort by number of affected pages (descending)
        self.findings.keyword_cannibalization.sort(
            key=lambda x: x['page_count'], reverse=True
        )
        
        logger.debug(f"Found {len(self.findings.keyword_cannibalization)} keyword cannibalization instances")
    
    def _detect_duplicate_h1s(self):
        """Find pages with identical or very similar H1 tags."""
        h1_pages = defaultdict(list)
        
        for summary in self.summaries:
            if summary.h1_text:
                # Normalize H1 (lowercase, strip extra whitespace)
                h1 = ' '.join(summary.h1_text.lower().split())
                if h1:
                    h1_pages[h1].append(summary.filename)
        
        for h1, pages in h1_pages.items():
            if len(pages) > 1:
                self.findings.duplicate_h1s.append({
                    'h1_text': h1,
                    'affected_pages': pages,
                    'page_count': len(pages),
                    'severity': 'medium',
                    'recommendation': "Ensure each page has a unique, descriptive H1"
                })
        
        logger.debug(f"Found {len(self.findings.duplicate_h1s)} duplicate H1s")
    
    def _detect_thin_content(self):
        """Find pages with unusually low word counts."""
        word_counts = [(s.filename, s.word_count) for s in self.summaries 
                       if s.word_count is not None and s.word_count > 0]
        
        if len(word_counts) < 3:
            return
        
        counts = [wc[1] for wc in word_counts]
        avg = mean(counts)
        threshold = avg * 0.3  # Less than 30% of average = thin
        
        for filename, wc in word_counts:
            if wc < threshold or wc < 300:  # Absolute minimum threshold
                self.findings.thin_content_pages.append(filename)
        
        logger.debug(f"Found {len(self.findings.thin_content_pages)} thin content pages")
    
    def _detect_repeated_errors(self):
        """Find spelling/grammar errors that appear on multiple pages."""
        error_pages = defaultdict(list)
        
        for summary in self.summaries:
            errors = summary.extra.get('spelling_errors', []) + \
                     summary.extra.get('grammar_errors', [])
            
            for error in errors:
                if isinstance(error, dict):
                    error_text = error.get('original', error.get('text', ''))
                elif isinstance(error, str):
                    error_text = error
                else:
                    continue
                
                if error_text:
                    error_pages[error_text.lower()].append(summary.filename)
        
        for error, pages in error_pages.items():
            if len(pages) > 1:
                self.findings.repeated_errors.append({
                    'error': error,
                    'affected_pages': pages,
                    'page_count': len(pages),
                    'severity': 'high' if len(pages) > 3 else 'medium',
                    'recommendation': "Likely a template issue - fix in source template"
                })
        
        self.findings.repeated_errors.sort(key=lambda x: x['page_count'], reverse=True)
        logger.debug(f"Found {len(self.findings.repeated_errors)} repeated errors")
    
    def _detect_style_inconsistencies(self):
        """Detect mixed British/American English or inconsistent style."""
        british_indicators = ['colour', 'favourite', 'organisation', 'centre', 'licence']
        american_indicators = ['color', 'favorite', 'organization', 'center', 'license']
        
        british_pages = []
        american_pages = []
        
        for summary in self.summaries:
            errors = summary.extra.get('spelling_errors', [])
            style_notes = summary.extra.get('style_notes', [])
            
            # Check for style indicators
            all_text = ' '.join([str(e) for e in errors + style_notes]).lower()
            
            if any(ind in all_text for ind in british_indicators):
                british_pages.append(summary.filename)
            if any(ind in all_text for ind in american_indicators):
                american_pages.append(summary.filename)
        
        if british_pages and american_pages:
            self.findings.style_inconsistencies.append({
                'issue': 'Mixed British/American English',
                'british_style_pages': british_pages,
                'american_style_pages': american_pages,
                'severity': 'medium',
                'recommendation': 'Standardize on one English variant across all pages'
            })
    
    def _detect_score_outliers(self):
        """Find pages with scores significantly different from the average."""
        scores = [(s.filename, s.score) for s in self.summaries 
                  if s.score is not None]
        
        if len(scores) < 5:
            return
        
        score_values = [s[1] for s in scores]
        avg = mean(score_values)
        std = stdev(score_values) if len(score_values) > 1 else 0
        
        if std == 0:
            return
        
        for filename, score in scores:
            z_score = (score - avg) / std
            if abs(z_score) > 2:  # More than 2 standard deviations
                self.findings.score_outliers.append({
                    'filename': filename,
                    'score': score,
                    'z_score': round(z_score, 2),
                    'direction': 'above_average' if z_score > 0 else 'below_average',
                    'severity': 'high' if abs(z_score) > 3 else 'medium'
                })
        
        self.findings.score_outliers.sort(key=lambda x: abs(x['z_score']), reverse=True)
    
    def _detect_entity_inconsistencies(self):
        """Detect inconsistent entity/brand naming across pages (GEO audit)."""
        entity_variations = defaultdict(lambda: defaultdict(list))
        
        for summary in self.summaries:
            entities = summary.extra.get('entities', [])
            for entity in entities:
                if isinstance(entity, dict):
                    name = entity.get('name', '')
                    entity_type = entity.get('type', 'unknown')
                elif isinstance(entity, str):
                    name = entity
                    entity_type = 'unknown'
                else:
                    continue
                
                # Group by lowercase version to find variations
                key = name.lower().strip()
                if key:
                    entity_variations[key][name].append(summary.filename)
        
        # Find entities with multiple spellings/capitalizations
        for key, variations in entity_variations.items():
            if len(variations) > 1:
                self.findings.terminology_variations.append({
                    'base_term': key,
                    'variations': dict(variations),
                    'severity': 'medium',
                    'recommendation': f"Standardize naming for '{key}'"
                })
    
    def _detect_tone_drift(self):
        """Detect pages with different tones (BRAND_VOICE audit)."""
        tones = defaultdict(list)
        
        for summary in self.summaries:
            tone = summary.extra.get('tone')
            if tone:
                tones[tone.lower()].append(summary.filename)
        
        if len(tones) > 1:
            # Find the dominant tone
            dominant_tone = max(tones.items(), key=lambda x: len(x[1]))
            
            for tone, pages in tones.items():
                if tone != dominant_tone[0]:
                    self.findings.score_outliers.append({
                        'issue': f"Tone drift: '{tone}' (dominant: '{dominant_tone[0]}')",
                        'affected_pages': pages,
                        'severity': 'medium' if len(pages) < 3 else 'high'
                    })
    
    def _detect_terminology_inconsistencies(self):
        """Detect same concept described with different words (BRAND_VOICE)."""
        term_pages = defaultdict(list)
        
        for summary in self.summaries:
            terminology = summary.extra.get('terminology', [])
            for term in terminology:
                if isinstance(term, str):
                    term_pages[term.lower()].append(summary.filename)
    
    def _detect_claim_contradictions(self):
        """Detect potentially contradicting claims across pages."""
        claims_by_topic = defaultdict(list)
        
        for summary in self.summaries:
            claims = summary.extra.get('environmental_claims', []) or \
                     summary.extra.get('claims', [])
            
            for claim in claims:
                if isinstance(claim, dict):
                    topic = claim.get('topic', claim.get('category', 'general'))
                    text = claim.get('text', claim.get('claim', ''))
                elif isinstance(claim, str):
                    topic = 'general'
                    text = claim
                else:
                    continue
                
                if text:
                    claims_by_topic[topic].append({
                        'claim': text,
                        'page': summary.filename
                    })
        
        # Flag topics with multiple distinct claims (potential contradictions)
        for topic, claims in claims_by_topic.items():
            if len(claims) > 2:
                self.findings.score_outliers.append({
                    'issue': f"Multiple claims about '{topic}' - review for consistency",
                    'claims': claims,
                    'severity': 'medium'
                })

    def _detect_missing_schema_patterns(self):
        """Detect pages that lack structured data where it is commonly expected."""
        missing_schema_pages = []
        for summary in self.summaries:
            schema_types = summary.extra.get('schema_types', [])
            if not schema_types:
                missing_schema_pages.append(summary.filename)
        if missing_schema_pages:
            self.findings.repeated_errors.append({
                'pattern': 'Missing structured data (schema.org)',
                'affected_pages': missing_schema_pages,
                'count': len(missing_schema_pages),
                'severity': 'major',
                'recommendation': 'Add JSON-LD schema markup (Article, Product, FAQ, etc.) to all pages.'
            })

    def _detect_missing_video_schema(self):
        """Flag pages with video embeds but no VideoObject schema."""
        missing_video_schema = []
        for summary in self.summaries:
            if summary.extra.get('has_video_schema') is False:
                missing_video_schema.append(summary.filename)
        if missing_video_schema:
            self.findings.repeated_errors.append({
                'pattern': 'Video embeds without VideoObject schema',
                'affected_pages': missing_video_schema,
                'count': len(missing_video_schema),
                'severity': 'major',
                'recommendation': 'Add VideoObject JSON-LD with name, description, thumbnailUrl, uploadDate, and contentUrl/embedUrl.'
            })

    def _detect_orphan_pages(self):
        """Detect pages with zero body content internal links (orphan risk)."""
        orphan_candidates = []
        for summary in self.summaries:
            body_links = summary.extra.get('body_links_count', None)
            if body_links is not None and body_links == 0:
                orphan_candidates.append(summary.filename)
        if orphan_candidates:
            self.findings.thin_content_pages.extend(orphan_candidates)
            self.findings.repeated_errors.append({
                'pattern': 'Pages with no body-content internal links (orphan risk)',
                'affected_pages': orphan_candidates,
                'count': len(orphan_candidates),
                'severity': 'major',
                'recommendation': 'Add contextual internal links to and from these pages within body content.'
            })

    def _detect_anchor_text_homogeneity(self):
        """Flag sites where generic anchors dominate across many pages."""
        pages_with_generic = []
        for summary in self.summaries:
            generic_anchors = summary.extra.get('generic_anchors', [])
            if generic_anchors:
                pages_with_generic.append({
                    'page': summary.filename,
                    'generic_anchors': generic_anchors[:5]
                })
        if len(pages_with_generic) > len(self.summaries) * 0.4:
            self.findings.style_inconsistencies.append({
                'issue': "Generic anchor text ('click here', 'read more', 'here') is site-wide pattern",
                'affected_page_count': len(pages_with_generic),
                'sample_pages': pages_with_generic[:5],
                'recommendation': 'Replace all generic anchors with keyword-descriptive text.'
            })

    def _detect_ai_content_clusters(self):
        """Detect clusters of pages with suspiciously similar low EEAT scores."""
        low_eeat_pages = []
        for summary in self.summaries:
            eeat = summary.extra.get('eeat_score')
            if eeat is not None and eeat < 40:
                low_eeat_pages.append({'page': summary.filename, 'eeat_score': eeat})
        if len(low_eeat_pages) >= 3:
            self.findings.repeated_errors.append({
                'pattern': 'Multiple pages with very low E-E-A-T scores (possible AI-generated thin content)',
                'affected_pages': [p['page'] for p in low_eeat_pages],
                'count': len(low_eeat_pages),
                'severity': 'critical',
                'recommendation': 'Audit these pages for AI-generated content and add author expertise, citations, and original insights.'
            })

    def _detect_repeated_accessibility_violations(self):
        """Detect accessibility violations that appear across many pages (template-level issues)."""
        violation_counter: Counter = Counter()
        page_map: Dict[str, List[str]] = defaultdict(list)
        for summary in self.summaries:
            for violation in summary.extra.get('critical_violations', []):
                key = str(violation)[:120]
                violation_counter[key] += 1
                page_map[key].append(summary.filename)
        for violation, count in violation_counter.most_common(5):
            if count >= 3:
                self.findings.repeated_errors.append({
                    'pattern': f'Repeated accessibility violation across {count} pages',
                    'violation': violation,
                    'affected_pages': page_map[violation][:10],
                    'count': count,
                    'severity': 'critical',
                    'recommendation': 'This appears to be a template-level issue — fix in the shared layout/component.'
                })

    def _detect_nap_inconsistencies(self):
        """Detect inconsistent NAP (Name/Address/Phone) signals across Local SEO pages."""
        nap_scores = [
            (s.filename, s.extra.get('nap_consistency'))
            for s in self.summaries
            if s.extra.get('nap_consistency') is not None
        ]
        low_nap = [(f, score) for f, score in nap_scores if score < 60]
        if low_nap:
            self.findings.terminology_variations.append({
                'issue': 'NAP (Name/Address/Phone) consistency issues detected across pages',
                'affected_pages': [f for f, _ in low_nap],
                'count': len(low_nap),
                'severity': 'major',
                'recommendation': 'Standardize business name, address, and phone format across all pages and match Google Business Profile exactly.'
            })

    def _detect_repeated_issues_generic(self):
        """Generic repeated-issue detection for audit types without specialized logic."""
        issue_counter: Counter = Counter()
        page_map: Dict[str, List[str]] = defaultdict(list)
        for summary in self.summaries:
            for issue_text in summary.top_issues:
                if issue_text:
                    key = issue_text[:100]
                    issue_counter[key] += 1
                    page_map[key].append(summary.filename)
        for issue, count in issue_counter.most_common(10):
            if count >= max(2, len(self.summaries) // 4):
                self.findings.repeated_errors.append({
                    'pattern': issue,
                    'affected_pages': page_map[issue][:15],
                    'count': count,
                    'severity': 'major' if count >= len(self.summaries) // 2 else 'minor',
                    'recommendation': f'This issue appears on {count} pages — likely a template or systemic problem. Prioritize a site-wide fix.'
                })


# ============================================================================
# STATISTICS CALCULATOR
# ============================================================================
def calculate_statistics(summaries: List[PageSummary]) -> CrossRefStats:
    """Calculate statistics from page summaries."""
    stats = CrossRefStats()
    stats.pages_analyzed = len(summaries)
    
    # Extract scores
    scores = [(s.filename, s.score) for s in summaries if s.score is not None]
    
    if not scores:
        return stats
    
    score_values = [s[1] for s in scores]
    
    stats.average_score = round(mean(score_values), 1)
    stats.median_score = round(median(score_values), 1)
    stats.std_deviation = round(stdev(score_values), 1) if len(score_values) > 1 else 0
    stats.min_score = min(score_values)
    stats.max_score = max(score_values)
    
    # Score distribution buckets
    buckets = {
        '0-25': 0, '26-50': 0, '51-75': 0, '76-100': 0
    }
    for score in score_values:
        if score <= 25:
            buckets['0-25'] += 1
        elif score <= 50:
            buckets['26-50'] += 1
        elif score <= 75:
            buckets['51-75'] += 1
        else:
            buckets['76-100'] += 1
    stats.score_distribution = buckets
    
    # Top and bottom 5 pages
    sorted_scores = sorted(scores, key=lambda x: x[1], reverse=True)
    stats.top_5_pages = [{'filename': f, 'score': s} for f, s in sorted_scores[:5]]
    stats.bottom_5_pages = [{'filename': f, 'score': s} for f, s in sorted_scores[-5:]]
    
    return stats


# ============================================================================
# SMART AGGREGATION (For LLM Context Window)
# ============================================================================
def create_aggregated_summary(
    summaries: List[PageSummary],
    stats: CrossRefStats,
    rule_findings: RuleBasedFindings,
    max_tokens: int = 15000
) -> Dict[str, Any]:
    """
    Create a compressed summary suitable for LLM analysis.
    
    Keeps total under max_tokens (~15000 tokens = ~60000 chars).
    """
    # Estimate chars (rough: 4 chars per token)
    max_chars = max_tokens * 4
    
    summary = {
        'overview': {
            'pages_analyzed': stats.pages_analyzed,
            'average_score': stats.average_score,
            'median_score': stats.median_score,
            'std_deviation': stats.std_deviation,
            'score_range': f"{stats.min_score}-{stats.max_score}",
            'score_distribution': stats.score_distribution
        },
        'top_pages': stats.top_5_pages,
        'bottom_pages': stats.bottom_5_pages,
        'rule_based_findings': {
            'keyword_cannibalization': rule_findings.keyword_cannibalization[:10],
            'duplicate_h1s': rule_findings.duplicate_h1s[:5],
            'repeated_errors': rule_findings.repeated_errors[:10],
            'thin_content_pages': rule_findings.thin_content_pages[:10],
            'style_inconsistencies': rule_findings.style_inconsistencies[:5],
            'score_outliers': rule_findings.score_outliers[:10],
            'terminology_variations': rule_findings.terminology_variations[:5]
        }
    }
    
    # Add per-page summaries (compressed)
    page_summaries = []
    for s in summaries:
        page_data = {
            'file': s.filename,
            'score': s.score,
        }
        if s.primary_keyword:
            page_data['keyword'] = s.primary_keyword
        if s.top_issues:
            page_data['issues'] = s.top_issues[:2]
        if s.content_type:
            page_data['type'] = s.content_type
        page_summaries.append(page_data)
    
    # Calculate current size and truncate if needed
    current_json = json.dumps(summary, indent=2)
    remaining_chars = max_chars - len(current_json) - 1000  # Buffer
    
    if remaining_chars > 0:
        # Add as many page summaries as will fit
        pages_json = json.dumps(page_summaries)
        if len(pages_json) < remaining_chars:
            summary['page_summaries'] = page_summaries
        else:
            # Truncate to fit
            chars_per_page = len(pages_json) // len(page_summaries)
            max_pages = remaining_chars // chars_per_page
            summary['page_summaries'] = page_summaries[:max_pages]
            summary['note'] = f"Showing {max_pages} of {len(page_summaries)} pages (truncated)"
    
    # Collect all unique keywords for cannibalization analysis
    all_keywords = Counter()
    for s in summaries:
        if s.primary_keyword:
            all_keywords[s.primary_keyword.lower()] += 1
        for kw in s.secondary_keywords:
            all_keywords[kw.lower()] += 1
    
    summary['keyword_frequency'] = dict(all_keywords.most_common(30))
    
    # Collect common issues
    issue_counter = Counter()
    for s in summaries:
        for issue in s.top_issues:
            if issue:
                issue_counter[issue] += 1
    summary['common_issues'] = dict(issue_counter.most_common(15))
    
    return summary


# ============================================================================
# LLM CROSS-REFERENCE ANALYSIS
# ============================================================================
CROSS_REF_SYSTEM_PROMPTS = {
    'SEO_AUDIT': """You are a Senior SEO Strategist performing a site-wide cross-page analysis.

Analyze the aggregated audit data for patterns, issues, and strategic opportunities.

Focus on:
1. Keyword cannibalization (multiple pages competing for same keyword)
2. Content gaps (topics not covered that should be)
3. Internal linking opportunities between related pages
4. Pages that should be consolidated or removed
5. Site structure and information architecture issues

Provide actionable, prioritized recommendations with estimated impact.""",

    'GEO_AUDIT': """You are a GEO (Generative Engine Optimization) Specialist analyzing site-wide patterns.

Focus on:
1. Entity and brand name consistency across pages
2. Topics with high citation potential vs gaps
3. Authority clustering - which content areas are strongest
4. Fact density and sourcing patterns
5. Opportunities to increase AI citation likelihood

Provide recommendations to improve the site's visibility in AI-generated responses.""",

    'BRAND_VOICE': """You are a Brand Voice Analyst reviewing site-wide consistency.

Focus on:
1. Tone drift - pages that deviate from the dominant brand voice
2. Terminology inconsistencies - same concepts described differently
3. Voice score variance patterns
4. Formality level consistency
5. Key messaging alignment

Provide recommendations to strengthen brand voice consistency.""",

    'SPELLING_GRAMMAR': """You are a Copy Editor analyzing site-wide writing quality patterns.

Focus on:
1. Repeated errors (likely template issues)
2. Style inconsistencies (British vs American English)
3. Common grammar patterns to fix
4. Pages that need prioritized editing
5. Training recommendations for content writers

Provide a prioritized action plan for improving site-wide writing quality.""",

    'GREENWASHING': """You are a Sustainability Communications Compliance Expert.

Focus on:
1. Contradicting environmental claims across pages
2. Unsubstantiated claims patterns
3. Missing evidence or certifications
4. Compliance gaps in product vs marketing pages
5. Risk assessment for regulatory issues

Provide recommendations to ensure environmental claims are accurate and compliant.""",

    'ADVERTISMENT': """You are an Advertising Compliance Specialist.

Focus on:
1. Contradicting claims across different pages
2. Missing disclosures or disclaimers
3. Compliance page linkage from product pages
4. Patterns in advertising violations
5. Regulatory risk assessment

Provide recommendations to ensure advertising compliance across the site.""",

    'CONTENT_QUALITY': """You are a Senior Content Strategist analyzing site-wide content quality patterns.

Focus on:
1. E-E-A-T signal distribution — which pages are strong vs. thin in expertise and authority
2. Clusters of low-originality or AI-generated-looking content
3. Depth consistency — are some content areas systematically shallower than others?
4. Cite-worthiness gaps — where should the site be building authoritative reference content?
5. Content investment priorities — which pages have the highest score improvement potential?

Provide a prioritized content investment roadmap.""",

    'TECHNICAL_SEO': """You are a Technical SEO Specialist performing site-wide technical content analysis.

Focus on:
1. Schema markup coverage — which page types are systematically missing structured data?
2. Meta element quality patterns — title and description issues that span multiple pages
3. Video and media optimization gaps — embeds without schema or transcripts
4. Hreflang and multi-language signal consistency across the site
5. Mobile-first content structure issues at scale

Identify template-level technical fixes with the highest crawl and indexation impact.""",

    'UX_CONTENT': """You are a UX Content Specialist analyzing site-wide user experience patterns.

Focus on:
1. CTA consistency and effectiveness across pages — are key actions clear and compelling everywhere?
2. Navigation and wayfinding language — do users get lost or face unclear paths?
3. Trust signal distribution — where are trust signals missing vs. over-concentrated?
4. Audience specificity — which pages try to speak to everyone and end up speaking to no one?
5. Friction point patterns — what UX content issues appear repeatedly across the site?

Provide recommendations to improve conversion-relevant content at scale.""",

    'ACCESSIBILITY_AUDIT': """You are an Accessibility and Inclusive Design Specialist analyzing site-wide patterns.

Focus on:
1. Repeated WCAG violations that appear across multiple pages (template-level issues)
2. Alt text coverage — what percentage of pages have systematic image accessibility gaps?
3. Heading structure consistency across page templates
4. Color contrast and font size signal issues detectable in content
5. Keyboard navigation and focus indicator gaps that appear site-wide

Prioritize template-level fixes that will resolve violations across the most pages simultaneously.""",

    'LEGAL_GDPR': """You are a Privacy and Legal Compliance Specialist analyzing site-wide compliance posture.

Focus on:
1. Cookie consent and privacy policy coverage — which page types lack required disclosures?
2. Data collection language consistency — do forms and landing pages have consistent privacy framing?
3. Blocker-severity violations that create regulatory risk across multiple pages
4. Missing legal page links (privacy policy, terms, cookie policy) from high-traffic pages
5. GDPR consent mechanism consistency across the site

Provide a compliance risk-ranked remediation plan.""",

    'INTERNAL_LINKING': """You are an Internal Linking and Site Architecture Specialist analyzing site-wide link structure.

Focus on:
1. Orphan page risk — pages with no body-content internal links that face crawlability issues
2. Anchor text diversity and quality — site-wide patterns of generic vs. descriptive anchors
3. Topic cluster gaps — content areas that lack hub-and-spoke structure
4. PageRank flow efficiency — is link equity being distributed to the highest-value pages?
5. Outbound link quality patterns — are external links consistently authoritative and relevant?

Provide a site architecture improvement plan with specific linking recommendations.""",

    'READABILITY_AUDIT': """You are a Content Readability Specialist analyzing site-wide writing quality.

Focus on:
1. Grade level consistency — does the site target an appropriate reading level for its audience?
2. Sentence length patterns — which content areas are systematically over-complex?
3. Passive voice density — where does passive voice undermine clarity at scale?
4. Vocabulary complexity — technical jargon overuse patterns across page types
5. Paragraph and structure patterns that consistently harm scannability

Provide content editor training recommendations and a prioritized rewriting plan.""",

    'COMPETITOR_ANALYSIS': """You are a Competitive Intelligence Strategist analyzing site-wide positioning.

Focus on:
1. Value proposition consistency — is the core brand promise consistent across all pages?
2. Generic claim clusters — where does the site systematically use category-default language?
3. Topical gap patterns — what subject areas is the site consistently failing to cover vs. competitors?
4. Differentiation signal strength — which pages own a distinctive position vs. which blend in?
5. Messaging hierarchy — is the strongest positioning in the most prominent positions?

Provide a site-wide competitive positioning improvement strategy.""",

    'CONTENT_FRESHNESS': """You are a Content Freshness and Editorial Calendar Specialist.

Focus on:
1. Staleness clusters — which topic areas or page types are systematically outdated?
2. Update urgency prioritization — rank pages by freshness risk and traffic importance
3. Evergreen vs. time-sensitive content balance — is the site over-indexed on dated content?
4. Missing recent developments — topic areas where competitors likely have fresher content
5. Content refresh calendar recommendations based on decay patterns

Provide a prioritized content refresh roadmap.""",

    'AI_OVERVIEW_OPTIMIZATION': """You are an AI Search Optimization Specialist analyzing site-wide AI visibility.

Focus on:
1. Featured snippet and AI Overview eligibility — which pages are closest to optimization?
2. Structured answer format adoption — where are FAQ, HowTo, and definition patterns missing?
3. Schema markup for AI visibility — FAQ and HowTo schema coverage gaps
4. Question-answer content patterns — which topics need explicit Q&A treatment?
5. Entity and fact density patterns — which content areas are most AI-citation-ready?

Provide recommendations to maximize the site's presence in AI-generated answers.""",

    'TRANSLATION_QUALITY': """You are a Multilingual Content Quality Specialist analyzing site-wide translation patterns.

Focus on:
1. Language quality consistency across translated page sets
2. Fluency and accuracy score distribution — which translated sections need priority re-translation?
3. Source language content quality — is poor-quality source content compounding translation problems?
4. Terminology consistency across translations — are key terms translated consistently?
5. Cultural adaptation gaps — where do translations lack appropriate localization?

Provide a translation quality improvement plan with priority ordering.""",

    'LOCAL_SEO': """You are a Local SEO Specialist analyzing site-wide local search optimization.

Focus on:
1. NAP (Name/Address/Phone) consistency across all pages and schema markup
2. Local Business schema coverage — which location pages lack structured data?
3. Location-specific content depth — do location pages have sufficient unique local signals?
4. Google Business Profile alignment — does on-page information match GBP data?
5. Local keyword and service area coverage patterns

Provide a local SEO remediation plan prioritized by location revenue potential.""",

    'SECURITY_CONTENT_AUDIT': """You are a Web Security Content Analyst reviewing site-wide security communication posture.

Focus on:
1. Inadvertent information exposure patterns — technical details that appear across multiple pages
2. Security page completeness — does the site communicate its security posture clearly?
3. Trust signal consistency — SSL indicators, privacy badges, and security certifications
4. Vulnerability disclosure and contact information — is responsible disclosure communicated?
5. High-risk content patterns (exposed emails, version numbers, stack details) at scale

Provide a prioritized security content remediation plan.""",

    'E_COMMERCE': """You are an E-Commerce Conversion Optimization Specialist analyzing site-wide store performance.

Focus on:
1. Conversion barrier patterns — which issues appear across the most product or category pages?
2. Trust signal distribution — where are purchase-enabling trust signals missing?
3. Product content quality clusters — which categories have systematically weak content?
4. CTA and checkout friction patterns at scale
5. Cross-sell and upsell content opportunities across the catalog

Provide a prioritized conversion optimization roadmap.""",

    'DEFAULT': """You are a Senior Website Auditor performing cross-page analysis.

Analyze the aggregated data for:
1. Site-wide patterns and issues
2. Inconsistencies across pages
3. Content gaps and opportunities
4. Pages requiring prioritized attention
5. Strategic recommendations

Provide actionable, prioritized recommendations."""
}


def get_cross_ref_prompt(audit_type: str) -> str:
    """Get the appropriate cross-reference system prompt for an audit type."""
    return CROSS_REF_SYSTEM_PROMPTS.get(
        audit_type.upper(), 
        CROSS_REF_SYSTEM_PROMPTS['DEFAULT']
    )


class LLMCrossRefAnalyzer:
    """Performs LLM-powered cross-reference analysis."""
    
    def __init__(self, provider: str, model_name: str):
        self.provider = provider.upper()
        self.model_name = model_name
        self._client = None
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize the appropriate async client."""
        if self.provider == "ANTHROPIC":
            api_key = os.getenv("ANTHROPIC_API_KEY")
            self._client = AsyncAnthropic(api_key=api_key)
        elif self.provider == "OPENAI":
            api_key = os.getenv("OPENAI_API_KEY")
            self._client = AsyncOpenAI(api_key=api_key)
        elif self.provider == "MISTRAL":
            api_key = os.getenv("MISTRAL_API_KEY")
            self._client = Mistral(api_key=api_key)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")
    
    async def analyze(
        self,
        website: str,
        audit_type: str,
        aggregated_summary: Dict[str, Any],
        max_tokens: int = 8192
    ) -> Dict[str, Any]:
        """
        Send aggregated data to LLM for cross-reference analysis.
        
        Returns structured analysis results.
        """
        system_prompt = get_cross_ref_prompt(audit_type)
        
        user_content = f"""Website: {website}
Audit Type: {audit_type}
Analysis Date: {datetime.now().strftime('%Y-%m-%d')}

AGGREGATED AUDIT DATA:
{json.dumps(aggregated_summary, indent=2)}

Analyze this data and provide a comprehensive site-wide analysis.

Return your analysis as JSON with this structure:
{{
  "site_wide_issues": [
    {{
      "issue": "string",
      "severity": "critical|high|medium|low",
      "affected_pages": ["filename1", "filename2"],
      "recommendation": "string",
      "estimated_impact": "string"
    }}
  ],
  "content_gaps": [
    {{
      "topic": "string",
      "rationale": "string",
      "priority": "high|medium|low"
    }}
  ],
  "consolidation_recommendations": [
    {{
      "pages_to_merge": ["page1", "page2"],
      "reason": "string",
      "proposed_action": "merge|redirect|delete"
    }}
  ],
  "priority_matrix": [
    {{
      "action": "string",
      "effort": "low|medium|high",
      "impact": "low|medium|high",
      "priority_score": 1-10
    }}
  ],
  "strategic_insights": "string",
  "quick_wins": ["string"]
}}"""

        try:
            if self.provider == "ANTHROPIC":
                response = await self._client.messages.create(
                    model=self.model_name,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}]
                )
                response_text = response.content[0].text
                
            elif self.provider == "OPENAI":
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens
                )
                response_text = response.choices[0].message.content
                
            elif self.provider == "MISTRAL":
                response = await self._client.chat.complete_async(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=max_tokens
                )
                response_text = response.choices[0].message.content
            
            # Parse JSON response
            # Handle potential markdown code blocks
            if '```json' in response_text:
                response_text = response_text.split('```json')[1].split('```')[0]
            elif '```' in response_text:
                response_text = response_text.split('```')[1].split('```')[0]
            
            return json.loads(response_text.strip())
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            return {
                'error': 'Failed to parse LLM response',
                'raw_response': response_text[:2000] if 'response_text' in locals() else 'No response'
            }
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            return {
                'error': str(e)
            }
    
    async def close(self):
        """Close the client connection."""
        if self.provider == "ANTHROPIC" and hasattr(self._client, 'close'):
            await self._client.close()
        elif self.provider == "OPENAI" and hasattr(self._client, 'close'):
            await self._client.close()


# ============================================================================
# MAIN CROSS-REFERENCE ANALYZER
# ============================================================================
class CrossReferenceAnalyzer:
    """
    Main orchestrator for cross-reference analysis.
    
    Aggregates individual audit results, runs rule-based analysis,
    and optionally sends to LLM for higher-level insights.
    """
    
    def __init__(
        self,
        website: str,
        audit_type: str,
        output_dir: Optional[str] = None,
        provider: Optional[str] = None,
        model_name: Optional[str] = None
    ):
        self.website = website
        self.audit_type = audit_type.upper()
        
        # Determine output directory
        if output_dir:
            self.output_dir = output_dir
        else:
            self.output_dir = os.path.join(website, f"output_{audit_type.lower()}")
        
        # Provider configuration
        self.provider = provider
        self.model_name = model_name
        
        # Data containers
        self.summaries: List[PageSummary] = []
        self.stats: Optional[CrossRefStats] = None
        self.rule_findings: Optional[RuleBasedFindings] = None
        self.llm_analysis: Optional[Dict[str, Any]] = None
    
    def load_audit_results(self) -> int:
        """
        Load all JSON audit results from the output directory.
        
        Returns:
            Number of files loaded
        """
        if not os.path.isdir(self.output_dir):
            logger.error(f"Output directory not found: {self.output_dir}")
            return 0
        
        extractor = get_extractor(self.audit_type)
        files_loaded = 0
        
        # Score prefix pattern (2-3 digits)
        score_pattern = re.compile(r'^(\d{2,3})')
        
        for filename in os.listdir(self.output_dir):
            if not filename.endswith('.json'):
                continue
            
            # Skip cross-reference output files
            if filename.startswith('CROSS_REFERENCE'):
                continue
            
            filepath = os.path.join(self.output_dir, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Extract summary
                if extractor:
                    summary = extractor(data, filename)
                else:
                    # Use generic extractor
                    audit_key = self.audit_type.lower().replace('_', '')
                    summary = AuditExtractor.extract_generic(data, filename, audit_key)
                
                # Try to extract score from filename if not in data
                if summary.score is None:
                    match = score_pattern.match(filename)
                    if match:
                        summary.score = int(match.group(1))
                
                self.summaries.append(summary)
                files_loaded += 1
                
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse {filename}: {e}")
            except Exception as e:
                logger.warning(f"Error loading {filename}: {e}")
        
        logger.info(f"Loaded {files_loaded} audit results from {self.output_dir}")
        return files_loaded
    
    def run_rule_based_analysis(self) -> RuleBasedFindings:
        """Run rule-based analysis (no LLM required)."""
        analyzer = RuleBasedAnalyzer(self.summaries, self.audit_type)
        self.rule_findings = analyzer.analyze_all()
        return self.rule_findings
    
    async def run_llm_analysis(self) -> Dict[str, Any]:
        """Run LLM-powered cross-reference analysis."""
        if not self.provider:
            # Auto-detect provider
            if os.getenv("ANTHROPIC_API_KEY"):
                self.provider = "ANTHROPIC"
                self.model_name = self.model_name or "claude-sonnet-4-20250514"
            elif os.getenv("OPENAI_API_KEY"):
                self.provider = "OPENAI"
                self.model_name = self.model_name or "gpt-4o"
            elif os.getenv("MISTRAL_API_KEY"):
                self.provider = "MISTRAL"
                self.model_name = self.model_name or "mistral-large-latest"
            else:
                raise ValueError("No API key found for LLM analysis")
        
        # Calculate statistics
        self.stats = calculate_statistics(self.summaries)
        
        # Run rule-based analysis first if not done
        if not self.rule_findings:
            self.run_rule_based_analysis()
        
        # Create aggregated summary
        aggregated = create_aggregated_summary(
            self.summaries,
            self.stats,
            self.rule_findings
        )
        
        # Run LLM analysis
        logger.info(f"Running LLM cross-reference analysis with {self.provider}...")
        
        llm_analyzer = LLMCrossRefAnalyzer(self.provider, self.model_name)
        try:
            self.llm_analysis = await llm_analyzer.analyze(
                self.website,
                self.audit_type,
                aggregated
            )
        finally:
            await llm_analyzer.close()
        
        return self.llm_analysis
    
    def generate_output(self, include_llm: bool = True) -> Dict[str, Any]:
        """
        Generate the final cross-reference analysis output.
        
        Args:
            include_llm: Whether to include LLM analysis results
        
        Returns:
            Complete cross-reference analysis dictionary
        """
        # Ensure statistics are calculated
        if not self.stats:
            self.stats = calculate_statistics(self.summaries)
        
        # Ensure rule-based analysis is done
        if not self.rule_findings:
            self.run_rule_based_analysis()
        
        output = {
            'cross_reference_analysis': {
                'website': self.website,
                'audit_type': self.audit_type,
                'pages_analyzed': len(self.summaries),
                'analysis_date': datetime.now().strftime('%Y-%m-%d'),
                'analysis_timestamp': datetime.now().isoformat(),
                
                'statistics': {
                    'average_score': self.stats.average_score,
                    'median_score': self.stats.median_score,
                    'std_deviation': self.stats.std_deviation,
                    'min_score': self.stats.min_score,
                    'max_score': self.stats.max_score,
                    'score_distribution': self.stats.score_distribution,
                    'top_5_pages': self.stats.top_5_pages,
                    'bottom_5_pages': self.stats.bottom_5_pages
                },
                
                'rule_based_findings': {
                    'keyword_cannibalization': self.rule_findings.keyword_cannibalization,
                    'duplicate_h1s': self.rule_findings.duplicate_h1s,
                    'repeated_errors': self.rule_findings.repeated_errors,
                    'thin_content_pages': self.rule_findings.thin_content_pages,
                    'style_inconsistencies': self.rule_findings.style_inconsistencies,
                    'score_outliers': self.rule_findings.score_outliers,
                    'terminology_variations': self.rule_findings.terminology_variations
                }
            }
        }
        
        if include_llm and self.llm_analysis:
            output['cross_reference_analysis']['llm_analysis'] = self.llm_analysis
        
        return output
    
    def save_output(self, include_llm: bool = True) -> str:
        """
        Save cross-reference analysis to JSON file.
        
        Returns:
            Path to saved file
        """
        output = self.generate_output(include_llm)
        
        # Determine output filename
        suffix = '' if include_llm else '_RULE_BASED_ONLY'
        output_filename = f"CROSS_REFERENCE_ANALYSIS{suffix}.json"
        output_path = os.path.join(self.output_dir, output_filename)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2)
        
        logger.info(f"Cross-reference analysis saved to: {output_path}")
        return output_path
    
    def print_summary(self):
        """Print a summary of findings to console."""
        print("\n" + "="*80)
        print(f"CROSS-REFERENCE ANALYSIS: {self.website}")
        print(f"Audit Type: {self.audit_type}")
        print("="*80)
        
        # Statistics
        if self.stats:
            print(f"\n📊 STATISTICS")
            print(f"   Pages analyzed: {self.stats.pages_analyzed}")
            print(f"   Average score: {self.stats.average_score}")
            print(f"   Median score: {self.stats.median_score}")
            print(f"   Score range: {self.stats.min_score} - {self.stats.max_score}")
            print(f"   Std deviation: {self.stats.std_deviation}")
        
        # Rule-based findings
        if self.rule_findings:
            print(f"\n🔍 RULE-BASED FINDINGS")
            
            if self.rule_findings.keyword_cannibalization:
                print(f"   Keyword cannibalization: {len(self.rule_findings.keyword_cannibalization)} instances")
                for kc in self.rule_findings.keyword_cannibalization[:3]:
                    print(f"      - '{kc['keyword']}' → {kc['page_count']} pages")
            
            if self.rule_findings.duplicate_h1s:
                print(f"   Duplicate H1s: {len(self.rule_findings.duplicate_h1s)} instances")
            
            if self.rule_findings.repeated_errors:
                print(f"   Repeated errors: {len(self.rule_findings.repeated_errors)} patterns")
            
            if self.rule_findings.thin_content_pages:
                print(f"   Thin content pages: {len(self.rule_findings.thin_content_pages)}")
            
            if self.rule_findings.score_outliers:
                print(f"   Score outliers: {len(self.rule_findings.score_outliers)}")
        
        # LLM findings
        if self.llm_analysis and 'site_wide_issues' in self.llm_analysis:
            issues = self.llm_analysis['site_wide_issues']
            print(f"\n🤖 LLM ANALYSIS")
            print(f"   Site-wide issues identified: {len(issues)}")
            
            for issue in issues[:5]:
                severity = issue.get('severity', 'unknown')
                print(f"      [{severity.upper()}] {issue.get('issue', 'Unknown issue')}")
            
            if self.llm_analysis.get('quick_wins'):
                print(f"\n   Quick wins: {len(self.llm_analysis['quick_wins'])}")
                for qw in self.llm_analysis['quick_wins'][:3]:
                    print(f"      ✓ {qw}")
        
        print("\n" + "="*80)


# ============================================================================
# CLI INTERFACE
# ============================================================================
async def run_cross_reference_analysis(
    website: str,
    audit_type: str,
    output_dir: Optional[str] = None,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    no_llm: bool = False
) -> str:
    """
    Run cross-reference analysis on completed audit results.
    
    Args:
        website: Website domain
        audit_type: Type of audit (e.g., SEO_AUDIT, GEO_AUDIT)
        output_dir: Custom output directory (optional)
        provider: LLM provider (optional, auto-detected)
        model_name: Model name override (optional)
        no_llm: If True, run only rule-based analysis
    
    Returns:
        Path to the output file
    """
    analyzer = CrossReferenceAnalyzer(
        website=website,
        audit_type=audit_type,
        output_dir=output_dir,
        provider=provider,
        model_name=model_name
    )
    
    # Load results
    files_loaded = analyzer.load_audit_results()
    if files_loaded == 0:
        logger.error("No audit results found to analyze")
        return ""
    
    # Run rule-based analysis
    analyzer.run_rule_based_analysis()
    
    # Run LLM analysis if requested
    if not no_llm:
        await analyzer.run_llm_analysis()
    
    # Save and print results
    output_path = analyzer.save_output(include_llm=not no_llm)
    analyzer.print_summary()
    
    return output_path


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Cross-reference analyzer for site-wide pattern detection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Run full analysis (rule-based + LLM)
  python cross_reference_analyzer.py --website example.com --audit SEO_AUDIT
  
  # Run only rule-based analysis (free, faster)
  python cross_reference_analyzer.py --website example.com --audit SEO_AUDIT --no-llm
  
  # Use specific provider
  python cross_reference_analyzer.py --website example.com --audit GEO_AUDIT --provider anthropic
  
  # Custom output directory
  python cross_reference_analyzer.py --website example.com --audit BRAND_VOICE --output-dir ./custom/output
        '''
    )
    
    parser.add_argument(
        '--website',
        type=str,
        required=True,
        help='Target website domain'
    )
    
    parser.add_argument(
        '--audit',
        type=str,
        required=True,
        help='Audit type (e.g., SEO_AUDIT, GEO_AUDIT, BRAND_VOICE)'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Custom output directory (default: website/output_audittype/)'
    )
    
    parser.add_argument(
        '--provider',
        type=str,
        choices=['anthropic', 'openai', 'mistral'],
        help='LLM provider (default: auto-detect from API keys)'
    )
    
    parser.add_argument(
        '--model',
        type=str,
        help='Model name override'
    )
    
    parser.add_argument(
        '--no-llm',
        action='store_true',
        help='Run only rule-based analysis (faster, free)'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Logging level (default: INFO)'
    )
    
    return parser.parse_args()


if __name__ == "__main__":
    # Load environment
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
    
    args = parse_args()
    
    # Setup logging
    setup_logging(level=args.log_level)
    
    # Run analysis
    output_path = asyncio.run(run_cross_reference_analysis(
        website=args.website,
        audit_type=args.audit,
        output_dir=args.output_dir,
        provider=args.provider,
        model_name=args.model,
        no_llm=args.no_llm
    ))
    
    if output_path:
        print(f"\n✓ Analysis complete. Results saved to: {output_path}")
    else:
        print("\n✗ Analysis failed. Check logs for details.")
        sys.exit(1)
