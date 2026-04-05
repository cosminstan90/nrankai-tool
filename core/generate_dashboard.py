#!/usr/bin/env python3
"""
Generate Interactive HTML Dashboard from Audit Results.

Creates a self-contained HTML dashboard with charts, tables, and analytics
from Website LLM Analyzer audit outputs.

Author: Cosmin / Claude Assistant
Created: 2026-02-10
"""

import os
import re
import json
import argparse
import html as html_module
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from collections import Counter, defaultdict

# Import logger
try:
    from core.logger import get_logger, setup_logging
    logger = get_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


# ============================================================================
# AUDIT TYPE CONFIGURATIONS
# ============================================================================

AUDIT_CONFIGS = {
    # Score-based audits (0-100)
    'seo_audit': {
        'name': 'SEO Audit',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['seo_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['quick_wins'],
        'buckets': [
            (0, 49, 'poor', '#ef4444'),
            (50, 69, 'needs_work', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['content_depth_score', 'eeat_score', 'keyword_optimization_score', 'technical_onpage_score', 'image_seo_score', 'search_intent', 'estimated_useful_word_count', 'cannibalization_risk']
    },
    'geo_audit': {
        'name': 'GEO Audit',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['geo_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'low_citation', '#ef4444'),
            (50, 69, 'moderate', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'high_citation', '#22c55e')
        ],
        'extra_fields': ['citation_probability', 'authority_score', 'structure_score', 'factual_density_score']
    },
    'accessibility_audit': {
        'name': 'Accessibility Audit',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['accessibility_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['quick_wins'],
        'buckets': [
            (0, 49, 'non_compliant', '#ef4444'),
            (50, 69, 'partial', '#f97316'),
            (70, 84, 'mostly_compliant', '#eab308'),
            (85, 100, 'compliant', '#22c55e')
        ],
        'extra_fields': ['wcag_level', 'critical_issues_count']
    },
    'ux_content': {
        'name': 'UX Content',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['ux_content_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'poor', '#ef4444'),
            (50, 69, 'needs_work', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['message_clarity_score', 'cta_effectiveness_score', 'journey_alignment_score']
    },
    'legal_gdpr': {
        'name': 'Legal GDPR',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['gdpr_audit', 'overall_score'],
        'issues_path': ['violations'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'high_risk', '#ef4444'),
            (50, 69, 'medium_risk', '#f97316'),
            (70, 84, 'low_risk', '#eab308'),
            (85, 100, 'compliant', '#22c55e')
        ],
        'extra_fields': ['transparency_score', 'consent_quality_score', 'rights_communication_score']
    },
    'content_quality': {
        'name': 'Content Quality',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['content_quality', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['quick_wins'],
        'buckets': [
            (0, 49, 'thin', '#ef4444'),
            (50, 69, 'standard', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'high_quality', '#22c55e')
        ],
        'extra_fields': ['readability_score', 'depth_score', 'originality_score']
    },
    'brand_voice': {
        'name': 'Brand Voice',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['brand_voice_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'inconsistent', '#ef4444'),
            (50, 69, 'moderate', '#f97316'),
            (70, 84, 'consistent', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['consistency_score', 'distinctiveness_score', 'audience_alignment_score']
    },
    'e_commerce': {
        'name': 'E-Commerce',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['ecommerce_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'low_conversion', '#ef4444'),
            (50, 69, 'moderate', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'optimized', '#22c55e')
        ],
        'extra_fields': ['product_info_score', 'trust_signals_score', 'conversion_clarity_score']
    },
    'translation_quality': {
        'name': 'Translation Quality',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['translation_audit', 'overall_score'],
        'issues_path': ['errors'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'poor', '#ef4444'),
            (50, 69, 'acceptable', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['accuracy_score', 'fluency_score', 'localization_score']
    },
    'competitor_analysis': {
        'name': 'Competitor Analysis',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['competitive_positioning_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'lagging', '#ef4444'),
            (50, 69, 'average', '#f97316'),
            (70, 84, 'competitive', '#eab308'),
            (85, 100, 'leader', '#22c55e')
        ],
        'extra_fields': ['value_proposition_score', 'differentiation_score', 'messaging_score', 'topical_gap_score']
    },
    'relevancy_audit': {
        'name': 'Relevancy Audit',
        'type': 'score',
        'score_field': 'relevancy_score',
        'score_path': ['relevancy_audit', 'relevancy_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'obsolete', '#ef4444'),
            (50, 89, 'needs_update', '#f97316'),
            (90, 100, 'evergreen', '#22c55e')
        ],
        'extra_fields': ['content_freshness', 'last_update_signal']
    },
    
    # Violation-based audits
    'greenwashing': {
        'name': 'Greenwashing Audit',
        'type': 'violation',
        'violations_path': ['violations'],
        'severity_field': 'afm_principle_ref',
        'buckets': [
            (0, 0, 'compliant', '#22c55e'),
            (1, 3, 'minor_issues', '#eab308'),
            (4, 999, 'major_issues', '#ef4444')
        ],
        'severity_levels': ['1', '2', '3', '4', '5', '6']
    },
    'advertisment': {
        'name': 'Advertisement Compliance',
        'type': 'violation',
        'violations_path': ['violations'],
        'severity_field': 'severity',
        'buckets': [
            (0, 0, 'compliant', '#22c55e'),
            (1, 3, 'minor_issues', '#eab308'),
            (4, 999, 'major_issues', '#ef4444')
        ],
        'severity_levels': ['blocker', 'major', 'minor']
    },
    
    # Error count-based audits
    'spelling_grammar': {
        'name': 'Spelling & Grammar',
        'type': 'error_count',
        'errors_path': ['spelling_grammar_audit', 'errors'],
        'category_field': 'category',
        'buckets': [
            (0, 0, 'perfect', '#22c55e'),
            (1, 5, 'minor', '#eab308'),
            (6, 10, 'moderate', '#f97316'),
            (11, 999, 'major', '#ef4444')
        ],
        'error_types': ['spelling_mistake', 'grammar_mistake']
    },
    
    'readability_audit': {
        'name': 'Readability Audit',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['readability_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'very_difficult', '#ef4444'),
            (50, 69, 'difficult', '#f97316'),
            (70, 84, 'standard', '#eab308'),
            (85, 100, 'easy', '#22c55e')
        ],
        'extra_fields': ['flesch_score', 'avg_sentence_length', 'passive_voice_ratio']
    },
    'internal_linking': {
        'name': 'Internal Linking',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['internal_linking', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'poor', '#ef4444'),
            (50, 69, 'needs_work', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['anchor_quality_score', 'cluster_coverage_score', 'link_density_score', 'outbound_link_score']
    },
    'technical_seo': {
        'name': 'Technical SEO',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['technical_seo_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'critical_issues', '#ef4444'),
            (50, 69, 'needs_work', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'optimized', '#22c55e')
        ],
        'extra_fields': ['meta_elements_score', 'structured_data_score', 'mobile_signals_score', 'indexation_signals_score', 'video_media_score']
    },
    'content_freshness': {
        'name': 'Content Freshness',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['freshness_audit', 'overall_score'],
        'issues_path': ['obsolescence_indicators'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'stale', '#ef4444'),
            (50, 69, 'aging', '#f97316'),
            (70, 84, 'fresh', '#eab308'),
            (85, 100, 'evergreen', '#22c55e')
        ],
        'extra_fields': ['evergreen_ratio', 'score_justification']
    },
    'local_seo': {
        'name': 'Local SEO',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['local_seo_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'poor', '#ef4444'),
            (50, 69, 'needs_work', '#f97316'),
            (70, 84, 'good', '#eab308'),
            (85, 100, 'excellent', '#22c55e')
        ],
        'extra_fields': ['nap_consistency_score', 'schema_readiness_score', 'local_content_score']
    },
    'security_content_audit': {
        'name': 'Security Content Audit',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['security_content_audit', 'overall_score'],
        'issues_path': ['findings'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'high_risk', '#ef4444'),
            (50, 69, 'medium_risk', '#f97316'),
            (70, 84, 'low_risk', '#eab308'),
            (85, 100, 'secure', '#22c55e')
        ],
        'extra_fields': ['data_exposure_score', 'trust_signals_score', 'communication_quality_score']
    },
    'ai_overview_optimization': {
        'name': 'AI Overview Optimization',
        'type': 'score',
        'score_field': 'overall_score',
        'score_path': ['ai_overview_audit', 'overall_score'],
        'issues_path': ['issues'],
        'quick_wins_path': ['recommendations'],
        'buckets': [
            (0, 49, 'not_eligible', '#ef4444'),
            (50, 69, 'low_chance', '#f97316'),
            (70, 84, 'good_chance', '#eab308'),
            (85, 100, 'highly_eligible', '#22c55e')
        ],
        'extra_fields': ['paragraph_snippet_score', 'list_snippet_score', 'ai_overview_eligibility_score']
    },

    # Special scoring
    'kantar': {
        'name': 'Kantar MDS',
        'type': 'kantar',
        'score_path': ['kantar', 'mds_score'],
        'buckets': [
            (0, 5, 'low', '#ef4444'),
            (6, 12, 'medium', '#eab308'),
            (13, 20, 'high', '#22c55e')
        ],
        'extra_fields': ['brand_equity', 'market_penetration']
    }
}


# ============================================================================
# DATA EXTRACTION FUNCTIONS
# ============================================================================

def extract_score_from_filename(filename: str) -> Optional[int]:
    """Extract numerical score from filename prefix (e.g., '85_page.json' -> 85)."""
    match = re.match(r'^(\d{1,3})_', filename)
    if match:
        score = int(match.group(1))
        return score if 0 <= score <= 100 else None
    return None


def get_nested_value(data: dict, path: List[str], default=None):
    """Safely get a nested dictionary value using a path list."""
    current = data
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def _safe_int(value) -> Optional[int]:
    """Safely convert a value to int, handling strings like '58' and '/100' suffixes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().split('/')[0].strip()
        try:
            return int(float(cleaned))
        except (ValueError, TypeError):
            return None
    return None


def extract_score_from_json(data: dict, config: dict) -> Optional[int]:
    """Extract score from JSON data based on audit config."""
    if config['type'] == 'score' or config['type'] == 'kantar':
        score_path = config.get('score_path', [])
        score = get_nested_value(data, score_path)
        result = _safe_int(score)
        if result is not None:
            return result
    elif config['type'] == 'violation':
        violations = get_nested_value(data, config.get('violations_path', []), [])
        return len(violations) if isinstance(violations, list) else 0
    elif config['type'] == 'error_count':
        errors = get_nested_value(data, config.get('errors_path', []), [])
        return len(errors) if isinstance(errors, list) else 0
    return None


def extract_issues(data: dict, config: dict) -> List[dict]:
    """Extract issues/violations from JSON data."""
    if config['type'] == 'score':
        return get_nested_value(data, config.get('issues_path', []), []) or []
    elif config['type'] == 'violation':
        return get_nested_value(data, config.get('violations_path', []), []) or []
    elif config['type'] == 'error_count':
        return get_nested_value(data, config.get('errors_path', []), []) or []
    return []


def extract_quick_wins(data: dict, config: dict) -> List[str]:
    """Extract quick wins from JSON data."""
    quick_wins_path = config.get('quick_wins_path', [])
    if quick_wins_path:
        wins = get_nested_value(data, quick_wins_path, [])
        if isinstance(wins, list):
            return [str(w) for w in wins if w]
    return []


def get_bucket_for_score(score: int, buckets: List[Tuple]) -> Tuple[str, str]:
    """Get the bucket label and color for a given score."""
    for min_val, max_val, label, color in buckets:
        if min_val <= score <= max_val:
            return label, color
    return 'unknown', '#9ca3af'


def categorize_issue(issue: dict, config: dict) -> str:
    """Extract issue category/type for aggregation."""
    if config['type'] == 'score':
        # Try common category fields
        for field in ['category', 'type', 'issue_type', 'severity']:
            if field in issue:
                return str(issue[field])
        # Fallback to issue text truncated
        if 'issue' in issue:
            return str(issue['issue'])[:50]
    elif config['type'] == 'violation':
        return issue.get('violation_category', issue.get('rule_id', 'Unknown'))
    elif config['type'] == 'error_count':
        return issue.get('category', 'Unknown')
    return 'Unknown'


def get_issue_description(issue: dict, config: dict) -> str:
    """Get a readable description of an issue."""
    if config['type'] == 'score':
        return issue.get('issue', issue.get('recommendation', str(issue)))
    elif config['type'] == 'violation':
        return issue.get('reasoning', issue.get('fragment', str(issue)))[:100]
    elif config['type'] == 'error_count':
        return issue.get('error_description', issue.get('original_fragment', str(issue)))
    return str(issue)[:100]


# ============================================================================
# DATA LOADING AND PROCESSING
# ============================================================================

def load_audit_results(input_dir: str, audit_type: str) -> Tuple[List[dict], dict]:
    """
    Load all JSON audit results from the input directory.
    
    Returns:
        Tuple of (list of page results, metadata dict)
    """
    input_path = Path(input_dir)
    if not input_path.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    
    config = AUDIT_CONFIGS.get(audit_type.lower())
    if not config:
        # Try to auto-detect audit type from folder name
        folder_name = input_path.name.lower()
        for key in AUDIT_CONFIGS:
            if key in folder_name:
                audit_type = key
                config = AUDIT_CONFIGS[key]
                break
        if not config:
            # Default to generic score-based
            logger.warning(f"Unknown audit type '{audit_type}', using generic score config")
            config = AUDIT_CONFIGS['seo_audit'].copy()
            config['name'] = audit_type.replace('_', ' ').title()
    
    results = []
    json_files = list(input_path.glob('*.json'))
    
    logger.info(f"Loading {len(json_files)} JSON files from {input_dir}")
    
    for json_file in json_files:
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Extract score - first try filename, then JSON content
            score = extract_score_from_filename(json_file.name)
            if score is None:
                score = extract_score_from_json(data, config)
            
            # Get bucket classification
            bucket_label, bucket_color = ('unknown', '#9ca3af')
            if score is not None:
                bucket_label, bucket_color = get_bucket_for_score(score, config['buckets'])
            
            # Extract issues
            issues = extract_issues(data, config)
            
            # Extract quick wins
            quick_wins = extract_quick_wins(data, config)
            
            # Get top issue
            top_issue = ''
            if issues:
                top_issue = get_issue_description(issues[0], config)
            
            result = {
                'filename': json_file.name,
                'filepath': str(json_file),
                'score': score,
                'bucket_label': bucket_label,
                'bucket_color': bucket_color,
                'issues_count': len(issues),
                'issues': issues,
                'quick_wins': quick_wins,
                'top_issue': top_issue,
                'raw_data': data
            }
            
            results.append(result)
            
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse JSON from {json_file}: {e}")
        except Exception as e:
            logger.warning(f"Error processing {json_file}: {e}")
    
    # Sort by score (descending) if available
    results.sort(key=lambda x: (x['score'] is None, -(x['score'] or 0)))
    
    # Calculate metadata
    scores = [r['score'] for r in results if r['score'] is not None]
    metadata = {
        'total_pages': len(results),
        'pages_with_scores': len(scores),
        'avg_score': round(sum(scores) / len(scores), 1) if scores else None,
        'min_score': min(scores) if scores else None,
        'max_score': max(scores) if scores else None,
        'audit_type': audit_type,
        'config': config
    }
    
    return results, metadata


def aggregate_issues(results: List[dict], config: dict) -> List[dict]:
    """Aggregate issues across all pages."""
    issue_counter = Counter()
    
    for result in results:
        for issue in result['issues']:
            category = categorize_issue(issue, config)
            issue_counter[category] += 1
    
    # Return top 20 issues
    aggregated = [
        {'issue': issue, 'count': count}
        for issue, count in issue_counter.most_common(20)
    ]
    
    return aggregated


def aggregate_quick_wins(results: List[dict]) -> List[dict]:
    """Aggregate and deduplicate quick wins across all pages."""
    win_counter = Counter()
    
    for result in results:
        for win in result['quick_wins']:
            # Normalize the quick win text
            normalized = win.strip().lower()
            if normalized:
                win_counter[win] += 1
    
    # Return top 15 quick wins
    aggregated = [
        {'quick_win': win, 'frequency': count}
        for win, count in win_counter.most_common(15)
    ]
    
    return aggregated


def calculate_bucket_distribution(results: List[dict], config: dict) -> List[dict]:
    """Calculate distribution of pages across score buckets."""
    bucket_counts = defaultdict(int)
    
    for result in results:
        if result['score'] is not None:
            label, _ = get_bucket_for_score(result['score'], config['buckets'])
            bucket_counts[label] += 1
    
    distribution = []
    for min_val, max_val, label, color in config['buckets']:
        distribution.append({
            'label': label.replace('_', ' ').title(),
            'count': bucket_counts.get(label, 0),
            'color': color,
            'range': f"{min_val}-{max_val}"
        })
    
    return distribution


def find_historical_data(input_dir: str, audit_type: str) -> Optional[dict]:
    """Look for previous audit results to calculate trends."""
    input_path = Path(input_dir)
    parent_dir = input_path.parent
    
    # Look for other audit folders with timestamps or version numbers
    historical = []
    
    for folder in parent_dir.iterdir():
        if folder.is_dir() and audit_type.lower() in folder.name.lower():
            if folder != input_path:
                # Try to extract date from folder name
                date_match = re.search(r'(\d{4}[-_]\d{2}[-_]\d{2})', folder.name)
                if date_match:
                    historical.append({
                        'path': str(folder),
                        'date': date_match.group(1)
                    })
    
    if historical:
        historical.sort(key=lambda x: x['date'], reverse=True)
        return historical[0] if historical else None
    
    return None


# ============================================================================
# HTML TEMPLATE GENERATION
# ============================================================================

def generate_html_dashboard(
    results: List[dict],
    metadata: dict,
    website_name: str,
    output_path: str
) -> str:
    """Generate the complete HTML dashboard."""
    
    config = metadata['config']
    audit_name = config['name']
    audit_type = config['type']
    
    # Prepare data for charts
    bucket_distribution = calculate_bucket_distribution(results, config)
    aggregated_issues = aggregate_issues(results, config)
    aggregated_quick_wins = aggregate_quick_wins(results)
    
    # Prepare histogram data (for score-based audits)
    score_histogram = []
    if audit_type in ['score', 'kantar']:
        histogram_bins = defaultdict(int)
        for result in results:
            if result['score'] is not None:
                bin_start = (result['score'] // 10) * 10
                histogram_bins[bin_start] += 1
        for i in range(0, 110, 10):
            score_histogram.append({
                'range': f"{i}-{min(i+9, 100)}",
                'count': histogram_bins.get(i, 0)
            })
    
    # Prepare severity distribution (for violation-based audits)
    severity_distribution = []
    if audit_type == 'violation':
        severity_counts = Counter()
        for result in results:
            for issue in result['issues']:
                severity = issue.get(config.get('severity_field', 'severity'), 'Unknown')
                severity_counts[severity] += 1
        for severity, count in severity_counts.most_common():
            severity_distribution.append({
                'severity': str(severity),
                'count': count
            })
    
    # Prepare error type distribution (for error_count audits)
    error_type_distribution = []
    if audit_type == 'error_count':
        error_counts = Counter()
        for result in results:
            for issue in result['issues']:
                error_type = issue.get(config.get('category_field', 'category'), 'Unknown')
                error_counts[error_type] += 1
        for error_type, count in error_counts.most_common():
            error_type_distribution.append({
                'type': error_type.replace('_', ' ').title(),
                'count': count
            })
    
    # Prepare table data (sanitize for JSON embedding)
    table_data = []
    for i, result in enumerate(results):
        table_data.append({
            'id': i,
            'filename': result['filename'],
            'score': result['score'],
            'bucket_label': result['bucket_label'],
            'bucket_color': result['bucket_color'],
            'issues_count': result['issues_count'],
            'top_issue': result['top_issue'][:100] if result['top_issue'] else '',
            'raw_data': result['raw_data']
        })
    
    # Generate the HTML
    html_content = generate_html_template(
        website_name=website_name,
        audit_name=audit_name,
        audit_type=audit_type,
        metadata=metadata,
        bucket_distribution=bucket_distribution,
        score_histogram=score_histogram,
        severity_distribution=severity_distribution,
        error_type_distribution=error_type_distribution,
        aggregated_issues=aggregated_issues,
        aggregated_quick_wins=aggregated_quick_wins,
        table_data=table_data,
        config=config
    )
    
    return html_content


def generate_html_template(
    website_name: str,
    audit_name: str,
    audit_type: str,
    metadata: dict,
    bucket_distribution: List[dict],
    score_histogram: List[dict],
    severity_distribution: List[dict],
    error_type_distribution: List[dict],
    aggregated_issues: List[dict],
    aggregated_quick_wins: List[dict],
    table_data: List[dict],
    config: dict
) -> str:
    """Generate the HTML template with embedded data."""
    
    # Escape and serialize data for JavaScript
    def json_encode(data):
        return json.dumps(data, ensure_ascii=False, default=str)
    
    generation_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Determine score label based on audit type
    score_label = 'Score'
    if audit_type == 'violation':
        score_label = 'Violations'
    elif audit_type == 'error_count':
        score_label = 'Errors'
    
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html_module.escape(audit_name)} Dashboard - {html_module.escape(website_name)}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        :root {{
            --bg-primary: #ffffff;
            --bg-secondary: #f8fafc;
            --bg-tertiary: #f1f5f9;
            --text-primary: #1e293b;
            --text-secondary: #64748b;
            --text-muted: #94a3b8;
            --border-color: #e2e8f0;
            --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
            --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);
            --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1);
            --accent-color: #3b82f6;
            --success-color: #22c55e;
            --warning-color: #eab308;
            --danger-color: #ef4444;
            --radius-sm: 6px;
            --radius-md: 8px;
            --radius-lg: 12px;
        }}
        
        [data-theme="dark"] {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --border-color: #334155;
        }}
        
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background-color: var(--bg-secondary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 24px;
        }}
        
        /* Header */
        .header {{
            background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
            color: white;
            padding: 32px;
            border-radius: var(--radius-lg);
            margin-bottom: 24px;
            box-shadow: var(--shadow-lg);
        }}
        
        .header-content {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 20px;
        }}
        
        .header-title {{
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
        }}
        
        .header-subtitle {{
            font-size: 16px;
            opacity: 0.9;
        }}
        
        .header-stats {{
            display: flex;
            gap: 24px;
            flex-wrap: wrap;
        }}
        
        .stat-box {{
            text-align: center;
            padding: 12px 20px;
            background: rgba(255, 255, 255, 0.15);
            border-radius: var(--radius-md);
            backdrop-filter: blur(10px);
        }}
        
        .stat-value {{
            font-size: 28px;
            font-weight: 700;
        }}
        
        .stat-label {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            opacity: 0.85;
        }}
        
        .header-actions {{
            display: flex;
            gap: 12px;
            align-items: center;
        }}
        
        /* Theme Toggle */
        .theme-toggle {{
            background: rgba(255, 255, 255, 0.2);
            border: none;
            padding: 10px 16px;
            border-radius: var(--radius-md);
            color: white;
            cursor: pointer;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: background 0.2s;
        }}
        
        .theme-toggle:hover {{
            background: rgba(255, 255, 255, 0.3);
        }}
        
        /* Cards Grid */
        .cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 24px;
            margin-bottom: 24px;
        }}
        
        .card {{
            background: var(--bg-primary);
            border-radius: var(--radius-lg);
            box-shadow: var(--shadow-md);
            overflow: hidden;
            border: 1px solid var(--border-color);
        }}
        
        .card-header {{
            padding: 16px 20px;
            border-bottom: 1px solid var(--border-color);
            background: var(--bg-tertiary);
        }}
        
        .card-title {{
            font-size: 16px;
            font-weight: 600;
            color: var(--text-primary);
        }}
        
        .card-body {{
            padding: 20px;
        }}
        
        .card-full {{
            grid-column: 1 / -1;
        }}
        
        /* Charts */
        .chart-container {{
            position: relative;
            height: 280px;
            width: 100%;
        }}
        
        .chart-container-small {{
            height: 200px;
        }}
        
        /* Table */
        .table-controls {{
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        
        .search-input {{
            flex: 1;
            min-width: 200px;
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            font-size: 14px;
            background: var(--bg-primary);
            color: var(--text-primary);
        }}
        
        .search-input:focus {{
            outline: none;
            border-color: var(--accent-color);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
        }}
        
        .filter-select {{
            padding: 10px 16px;
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            font-size: 14px;
            background: var(--bg-primary);
            color: var(--text-primary);
            cursor: pointer;
        }}
        
        .table-wrapper {{
            overflow: hidden;
            border-radius: var(--radius-md);
            border: 1px solid var(--border-color);
        }}
        
        .table-scroll {{
            max-height: 500px;
            overflow-y: auto;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        
        th {{
            background: var(--bg-tertiary);
            padding: 12px 16px;
            text-align: left;
            font-weight: 600;
            color: var(--text-primary);
            position: sticky;
            top: 0;
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
        }}
        
        th:hover {{
            background: var(--border-color);
        }}
        
        th .sort-icon {{
            margin-left: 6px;
            opacity: 0.5;
        }}
        
        th.sorted .sort-icon {{
            opacity: 1;
        }}
        
        td {{
            padding: 12px 16px;
            border-top: 1px solid var(--border-color);
            vertical-align: top;
        }}
        
        tr:hover td {{
            background: var(--bg-tertiary);
        }}
        
        .score-badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 13px;
            color: white;
        }}
        
        .category-badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: var(--radius-sm);
            font-size: 12px;
            background: var(--bg-tertiary);
            color: var(--text-secondary);
        }}
        
        .issue-text {{
            color: var(--text-secondary);
            font-size: 13px;
            max-width: 300px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        
        .expand-btn {{
            background: none;
            border: 1px solid var(--border-color);
            padding: 4px 10px;
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 12px;
            color: var(--text-secondary);
            transition: all 0.2s;
        }}
        
        .expand-btn:hover {{
            background: var(--accent-color);
            color: white;
            border-color: var(--accent-color);
        }}
        
        /* Expanded Row */
        .expanded-content {{
            display: none;
            background: var(--bg-tertiary);
            padding: 16px;
            border-top: 1px solid var(--border-color);
        }}
        
        .expanded-content.show {{
            display: block;
        }}
        
        .json-viewer {{
            background: var(--bg-primary);
            border: 1px solid var(--border-color);
            border-radius: var(--radius-md);
            padding: 16px;
            max-height: 400px;
            overflow: auto;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 12px;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        
        /* Quick Wins & Issues List */
        .list-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            border-bottom: 1px solid var(--border-color);
        }}
        
        .list-item:last-child {{
            border-bottom: none;
        }}
        
        .list-item-text {{
            flex: 1;
            font-size: 14px;
            color: var(--text-primary);
            margin-right: 12px;
        }}
        
        .list-item-count {{
            background: var(--bg-tertiary);
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
            white-space: nowrap;
        }}
        
        /* Pagination */
        .pagination {{
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 8px;
            margin-top: 16px;
            flex-wrap: wrap;
        }}
        
        .page-btn {{
            padding: 8px 14px;
            border: 1px solid var(--border-color);
            background: var(--bg-primary);
            border-radius: var(--radius-sm);
            cursor: pointer;
            font-size: 14px;
            color: var(--text-primary);
            transition: all 0.2s;
        }}
        
        .page-btn:hover {{
            background: var(--bg-tertiary);
        }}
        
        .page-btn.active {{
            background: var(--accent-color);
            color: white;
            border-color: var(--accent-color);
        }}
        
        .page-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        
        .page-info {{
            color: var(--text-secondary);
            font-size: 14px;
        }}
        
        /* Footer */
        .footer {{
            text-align: center;
            padding: 24px;
            color: var(--text-muted);
            font-size: 13px;
        }}
        
        /* Print Styles */
        @media print {{
            body {{
                background: white;
                color: black;
            }}
            
            .container {{
                max-width: 100%;
                padding: 0;
            }}
            
            .header {{
                background: #3b82f6 !important;
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
            }}
            
            .theme-toggle,
            .expand-btn,
            .pagination {{
                display: none !important;
            }}
            
            .card {{
                break-inside: avoid;
                page-break-inside: avoid;
            }}
            
            .table-scroll {{
                max-height: none;
                overflow: visible;
            }}
            
            .cards-grid {{
                display: block;
            }}
            
            .card {{
                margin-bottom: 20px;
            }}
        }}
        
        /* Responsive */
        @media (max-width: 768px) {{
            .header-content {{
                flex-direction: column;
            }}
            
            .header-stats {{
                width: 100%;
                justify-content: space-between;
            }}
            
            .cards-grid {{
                grid-template-columns: 1fr;
            }}
            
            .table-controls {{
                flex-direction: column;
            }}
            
            .search-input {{
                width: 100%;
            }}
            
            table {{
                font-size: 12px;
            }}
            
            th, td {{
                padding: 8px 12px;
            }}
        }}
        
        /* Virtual Scroll Placeholder */
        .virtual-row {{
            height: 45px;
        }}
        
        /* Loading Spinner */
        .loading {{
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 40px;
        }}
        
        .spinner {{
            width: 40px;
            height: 40px;
            border: 3px solid var(--border-color);
            border-top-color: var(--accent-color);
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}
        
        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}
        
        /* Empty State */
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: var(--text-muted);
        }}
        
        .empty-state-icon {{
            font-size: 48px;
            margin-bottom: 16px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header class="header">
            <div class="header-content">
                <div>
                    <h1 class="header-title">{html_module.escape(audit_name)} Dashboard</h1>
                    <p class="header-subtitle">
                        {html_module.escape(website_name)} • Generated: {generation_date}
                    </p>
                </div>
                <div class="header-stats">
                    <div class="stat-box">
                        <div class="stat-value">{metadata['total_pages']}</div>
                        <div class="stat-label">Pages Analyzed</div>
                    </div>
                    {'<div class="stat-box"><div class="stat-value">' + str(metadata.get("avg_score", "N/A")) + '</div><div class="stat-label">Avg ' + score_label + '</div></div>' if metadata.get("avg_score") is not None else ''}
                    {'<div class="stat-box"><div class="stat-value">' + str(metadata.get("min_score", "N/A")) + ' - ' + str(metadata.get("max_score", "N/A")) + '</div><div class="stat-label">' + score_label + ' Range</div></div>' if metadata.get("min_score") is not None else ''}
                </div>
                <div class="header-actions">
                    <button class="theme-toggle" onclick="toggleTheme()">
                        <span id="theme-icon">🌙</span>
                        <span id="theme-text">Dark</span>
                    </button>
                </div>
            </div>
        </header>

        <!-- Charts Row -->
        <div class="cards-grid">
            <!-- Score Distribution (Donut) -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">📊 {score_label} Distribution</h2>
                </div>
                <div class="card-body">
                    <div class="chart-container">
                        <canvas id="distributionChart"></canvas>
                    </div>
                </div>
            </div>

            <!-- Histogram / Bar Chart -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">📈 {score_label} Breakdown</h2>
                </div>
                <div class="card-body">
                    <div class="chart-container">
                        <canvas id="histogramChart"></canvas>
                    </div>
                </div>
            </div>

            <!-- Top Issues -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">⚠️ Top Issues</h2>
                </div>
                <div class="card-body" style="padding: 0; max-height: 320px; overflow-y: auto;">
                    <div id="issuesList"></div>
                </div>
            </div>

            <!-- Quick Wins -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title">🎯 Quick Wins</h2>
                </div>
                <div class="card-body" style="padding: 0; max-height: 320px; overflow-y: auto;">
                    <div id="quickWinsList"></div>
                </div>
            </div>
        </div>

        <!-- Data Table -->
        <div class="card card-full">
            <div class="card-header">
                <h2 class="card-title">📋 Page Results</h2>
            </div>
            <div class="card-body">
                <div class="table-controls">
                    <input type="text" class="search-input" id="searchInput" 
                           placeholder="🔍 Search pages..." onkeyup="filterTable()">
                    <select class="filter-select" id="categoryFilter" onchange="filterTable()">
                        <option value="">All Categories</option>
                    </select>
                    <select class="filter-select" id="sortSelect" onchange="sortTable()">
                        <option value="score-desc">{score_label} (High to Low)</option>
                        <option value="score-asc">{score_label} (Low to High)</option>
                        <option value="issues-desc">Issues (Most)</option>
                        <option value="issues-asc">Issues (Least)</option>
                        <option value="name-asc">Filename (A-Z)</option>
                        <option value="name-desc">Filename (Z-A)</option>
                    </select>
                </div>
                <div class="table-wrapper">
                    <div class="table-scroll" id="tableScroll">
                        <table id="resultsTable">
                            <thead>
                                <tr>
                                    <th onclick="sortByColumn('filename')">Page <span class="sort-icon">↕</span></th>
                                    <th onclick="sortByColumn('score')">{score_label} <span class="sort-icon">↕</span></th>
                                    <th onclick="sortByColumn('bucket_label')">Category <span class="sort-icon">↕</span></th>
                                    <th onclick="sortByColumn('issues_count')">Issues <span class="sort-icon">↕</span></th>
                                    <th>Top Issue</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody">
                            </tbody>
                        </table>
                    </div>
                </div>
                <div class="pagination" id="pagination"></div>
            </div>
        </div>

        <!-- Footer -->
        <footer class="footer">
            Generated by Website LLM Analyzer Dashboard • {generation_date}
        </footer>
    </div>

    <!-- Embedded Data -->
    <script>
        // Audit configuration
        const AUDIT_TYPE = {json_encode(audit_type)};
        const SCORE_LABEL = {json_encode(score_label)};
        
        // Chart data
        const bucketDistribution = {json_encode(bucket_distribution)};
        const scoreHistogram = {json_encode(score_histogram)};
        const severityDistribution = {json_encode(severity_distribution)};
        const errorTypeDistribution = {json_encode(error_type_distribution)};
        const aggregatedIssues = {json_encode(aggregated_issues)};
        const aggregatedQuickWins = {json_encode(aggregated_quick_wins)};
        
        // Table data
        let tableData = {json_encode(table_data)};
        let filteredData = [...tableData];
        let currentPage = 1;
        const rowsPerPage = 50;
        let currentSort = {{ column: 'score', direction: 'desc' }};
        
        // Theme Management
        function toggleTheme() {{
            const html = document.documentElement;
            const isDark = html.getAttribute('data-theme') === 'dark';
            html.setAttribute('data-theme', isDark ? 'light' : 'dark');
            document.getElementById('theme-icon').textContent = isDark ? '🌙' : '☀️';
            document.getElementById('theme-text').textContent = isDark ? 'Dark' : 'Light';
            localStorage.setItem('theme', isDark ? 'light' : 'dark');
            updateChartColors();
        }}
        
        function initTheme() {{
            const savedTheme = localStorage.getItem('theme');
            if (savedTheme === 'dark' || (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {{
                document.documentElement.setAttribute('data-theme', 'dark');
                document.getElementById('theme-icon').textContent = '☀️';
                document.getElementById('theme-text').textContent = 'Light';
            }}
        }}
        
        // Chart Initialization
        let distributionChart, histogramChart;
        
        function getChartTextColor() {{
            return getComputedStyle(document.documentElement).getPropertyValue('--text-primary').trim() || '#1e293b';
        }}
        
        function getChartGridColor() {{
            return getComputedStyle(document.documentElement).getPropertyValue('--border-color').trim() || '#e2e8f0';
        }}
        
        function initCharts() {{
            const textColor = getChartTextColor();
            const gridColor = getChartGridColor();
            
            // Distribution Chart (Donut)
            const distCtx = document.getElementById('distributionChart').getContext('2d');
            distributionChart = new Chart(distCtx, {{
                type: 'doughnut',
                data: {{
                    labels: bucketDistribution.map(b => b.label + ' (' + b.range + ')'),
                    datasets: [{{
                        data: bucketDistribution.map(b => b.count),
                        backgroundColor: bucketDistribution.map(b => b.color),
                        borderWidth: 2,
                        borderColor: 'rgba(255,255,255,0.8)'
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{
                            position: 'bottom',
                            labels: {{ color: textColor, padding: 12 }}
                        }}
                    }},
                    cutout: '60%'
                }}
            }});
            
            // Histogram Chart
            const histCtx = document.getElementById('histogramChart').getContext('2d');
            
            let histData, histLabels, histColors;
            
            if (AUDIT_TYPE === 'score' || AUDIT_TYPE === 'kantar') {{
                histLabels = scoreHistogram.map(h => h.range);
                histData = scoreHistogram.map(h => h.count);
                histColors = scoreHistogram.map(h => {{
                    const score = parseInt(h.range.split('-')[0]);
                    if (score >= 85) return '#22c55e';
                    if (score >= 70) return '#eab308';
                    if (score >= 50) return '#f97316';
                    return '#ef4444';
                }});
            }} else if (AUDIT_TYPE === 'violation') {{
                histLabels = severityDistribution.map(s => s.severity);
                histData = severityDistribution.map(s => s.count);
                histColors = severityDistribution.map((_, i) => {{
                    const colors = ['#ef4444', '#f97316', '#eab308', '#22c55e'];
                    return colors[i % colors.length];
                }});
            }} else if (AUDIT_TYPE === 'error_count') {{
                histLabels = errorTypeDistribution.map(e => e.type);
                histData = errorTypeDistribution.map(e => e.count);
                histColors = ['#ef4444', '#f97316', '#eab308', '#22c55e', '#3b82f6'];
            }} else {{
                histLabels = bucketDistribution.map(b => b.label);
                histData = bucketDistribution.map(b => b.count);
                histColors = bucketDistribution.map(b => b.color);
            }}
            
            histogramChart = new Chart(histCtx, {{
                type: 'bar',
                data: {{
                    labels: histLabels,
                    datasets: [{{
                        label: 'Pages',
                        data: histData,
                        backgroundColor: histColors,
                        borderRadius: 4
                    }}]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        y: {{
                            beginAtZero: true,
                            ticks: {{ color: textColor }},
                            grid: {{ color: gridColor }}
                        }},
                        x: {{
                            ticks: {{ color: textColor }},
                            grid: {{ display: false }}
                        }}
                    }}
                }}
            }});
        }}
        
        function updateChartColors() {{
            const textColor = getChartTextColor();
            const gridColor = getChartGridColor();
            
            if (distributionChart) {{
                distributionChart.options.plugins.legend.labels.color = textColor;
                distributionChart.update();
            }}
            
            if (histogramChart) {{
                histogramChart.options.scales.y.ticks.color = textColor;
                histogramChart.options.scales.y.grid.color = gridColor;
                histogramChart.options.scales.x.ticks.color = textColor;
                histogramChart.update();
            }}
        }}
        
        // Lists Rendering
        function renderIssuesList() {{
            const container = document.getElementById('issuesList');
            if (!aggregatedIssues.length) {{
                container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">✨</div><p>No issues found!</p></div>';
                return;
            }}
            
            container.innerHTML = aggregatedIssues.map(item => `
                <div class="list-item">
                    <span class="list-item-text">${{escapeHtml(item.issue)}}</span>
                    <span class="list-item-count">${{item.count}} pages</span>
                </div>
            `).join('');
        }}
        
        function renderQuickWinsList() {{
            const container = document.getElementById('quickWinsList');
            if (!aggregatedQuickWins.length) {{
                container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🎉</div><p>No quick wins identified</p></div>';
                return;
            }}
            
            container.innerHTML = aggregatedQuickWins.map(item => `
                <div class="list-item">
                    <span class="list-item-text">${{escapeHtml(item.quick_win)}}</span>
                    <span class="list-item-count">${{item.frequency}}x</span>
                </div>
            `).join('');
        }}
        
        // Table Functions
        function populateFilters() {{
            const categories = [...new Set(tableData.map(d => d.bucket_label))].filter(Boolean);
            const select = document.getElementById('categoryFilter');
            categories.forEach(cat => {{
                const option = document.createElement('option');
                option.value = cat;
                option.textContent = cat.replace(/_/g, ' ').replace(/\\b\\w/g, l => l.toUpperCase());
                select.appendChild(option);
            }});
        }}
        
        function filterTable() {{
            const searchTerm = document.getElementById('searchInput').value.toLowerCase();
            const category = document.getElementById('categoryFilter').value;
            
            filteredData = tableData.filter(row => {{
                const matchesSearch = !searchTerm || 
                    row.filename.toLowerCase().includes(searchTerm) ||
                    (row.top_issue && row.top_issue.toLowerCase().includes(searchTerm));
                const matchesCategory = !category || row.bucket_label === category;
                return matchesSearch && matchesCategory;
            }});
            
            currentPage = 1;
            renderTable();
        }}
        
        function sortTable() {{
            const sortValue = document.getElementById('sortSelect').value;
            const [column, direction] = sortValue.split('-');
            
            filteredData.sort((a, b) => {{
                let valA, valB;
                
                switch(column) {{
                    case 'score':
                        valA = a.score ?? -1;
                        valB = b.score ?? -1;
                        break;
                    case 'issues':
                        valA = a.issues_count ?? 0;
                        valB = b.issues_count ?? 0;
                        break;
                    case 'name':
                        valA = a.filename.toLowerCase();
                        valB = b.filename.toLowerCase();
                        break;
                    default:
                        valA = a[column];
                        valB = b[column];
                }}
                
                if (typeof valA === 'string') {{
                    return direction === 'asc' ? valA.localeCompare(valB) : valB.localeCompare(valA);
                }}
                return direction === 'asc' ? valA - valB : valB - valA;
            }});
            
            currentPage = 1;
            renderTable();
        }}
        
        function sortByColumn(column) {{
            if (currentSort.column === column) {{
                currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
            }} else {{
                currentSort.column = column;
                currentSort.direction = 'desc';
            }}
            
            filteredData.sort((a, b) => {{
                let valA = a[column] ?? '';
                let valB = b[column] ?? '';
                
                if (typeof valA === 'number' && typeof valB === 'number') {{
                    return currentSort.direction === 'asc' ? valA - valB : valB - valA;
                }}
                return currentSort.direction === 'asc' 
                    ? String(valA).localeCompare(String(valB))
                    : String(valB).localeCompare(String(valA));
            }});
            
            renderTable();
        }}
        
        function renderTable() {{
            const tbody = document.getElementById('tableBody');
            const start = (currentPage - 1) * rowsPerPage;
            const end = start + rowsPerPage;
            const pageData = filteredData.slice(start, end);
            
            tbody.innerHTML = pageData.map(row => `
                <tr>
                    <td><strong>${{escapeHtml(row.filename)}}</strong></td>
                    <td>
                        <span class="score-badge" style="background-color: ${{row.bucket_color}}">
                            ${{row.score !== null ? row.score : 'N/A'}}
                        </span>
                    </td>
                    <td>
                        <span class="category-badge">${{escapeHtml(row.bucket_label.replace(/_/g, ' '))}}</span>
                    </td>
                    <td>${{row.issues_count}}</td>
                    <td class="issue-text" title="${{escapeHtml(row.top_issue)}}">${{escapeHtml(row.top_issue) || '-'}}</td>
                    <td>
                        <button class="expand-btn" onclick="toggleExpand(${{row.id}})">View Details</button>
                    </td>
                </tr>
                <tr id="expanded-${{row.id}}" style="display: none;">
                    <td colspan="6" style="padding: 0;">
                        <div class="expanded-content show">
                            <div class="json-viewer">${{formatJson(row.raw_data)}}</div>
                        </div>
                    </td>
                </tr>
            `).join('');
            
            renderPagination();
        }}
        
        function toggleExpand(id) {{
            const row = document.getElementById(`expanded-${{id}}`);
            if (row) {{
                row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
            }}
        }}
        
        function renderPagination() {{
            const totalPages = Math.ceil(filteredData.length / rowsPerPage);
            const pagination = document.getElementById('pagination');
            
            if (totalPages <= 1) {{
                pagination.innerHTML = '';
                return;
            }}
            
            let html = `
                <button class="page-btn" onclick="goToPage(1)" ${{currentPage === 1 ? 'disabled' : ''}}>&laquo;</button>
                <button class="page-btn" onclick="goToPage(${{currentPage - 1}})" ${{currentPage === 1 ? 'disabled' : ''}}>&lsaquo;</button>
            `;
            
            const maxVisible = 5;
            let startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
            let endPage = Math.min(totalPages, startPage + maxVisible - 1);
            
            if (endPage - startPage < maxVisible - 1) {{
                startPage = Math.max(1, endPage - maxVisible + 1);
            }}
            
            for (let i = startPage; i <= endPage; i++) {{
                html += `<button class="page-btn ${{i === currentPage ? 'active' : ''}}" onclick="goToPage(${{i}})">${{i}}</button>`;
            }}
            
            html += `
                <button class="page-btn" onclick="goToPage(${{currentPage + 1}})" ${{currentPage === totalPages ? 'disabled' : ''}}>&rsaquo;</button>
                <button class="page-btn" onclick="goToPage(${{totalPages}})" ${{currentPage === totalPages ? 'disabled' : ''}}>&raquo;</button>
                <span class="page-info">Page ${{currentPage}} of ${{totalPages}} (${{filteredData.length}} results)</span>
            `;
            
            pagination.innerHTML = html;
        }}
        
        function goToPage(page) {{
            const totalPages = Math.ceil(filteredData.length / rowsPerPage);
            if (page >= 1 && page <= totalPages) {{
                currentPage = page;
                renderTable();
                document.getElementById('tableScroll').scrollTop = 0;
            }}
        }}
        
        // Utility Functions
        function escapeHtml(text) {{
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}
        
        function formatJson(obj) {{
            try {{
                return escapeHtml(JSON.stringify(obj, null, 2));
            }} catch (e) {{
                return 'Error formatting JSON';
            }}
        }}
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            initTheme();
            initCharts();
            renderIssuesList();
            renderQuickWinsList();
            populateFilters();
            renderTable();
        }});
    </script>
</body>
</html>
'''
    return html


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate interactive HTML dashboard from audit results',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Generate dashboard from SEO audit results
  python generate_dashboard.py --input-dir ./example.com/output_seo_audit --audit-type seo_audit
  
  # Specify output file
  python generate_dashboard.py --input-dir ./site/output_geo_audit --audit-type geo_audit --output geo_dashboard.html
  
  # Auto-detect audit type from folder name
  python generate_dashboard.py --input-dir ./site/output_accessibility_audit

Supported audit types:
  Score-based:     seo_audit, geo_audit, accessibility_audit, ux_content, legal_gdpr,
                   content_quality, brand_voice, e_commerce, translation_quality,
                   competitor_analysis, relevancy_audit
  Violation-based: greenwashing, advertisment
  Error count:     spelling_grammar
  Special:         kantar
        '''
    )
    
    parser.add_argument(
        '--input-dir', '-i',
        type=str,
        required=True,
        help='Path to audit output directory containing JSON files'
    )
    
    parser.add_argument(
        '--audit-type', '-t',
        type=str,
        default=None,
        help='Audit type (auto-detected from folder name if not specified)'
    )
    
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=None,
        help='Output HTML file path (default: {website}_dashboard.html)'
    )
    
    parser.add_argument(
        '--website', '-w',
        type=str,
        default=None,
        help='Website name for the dashboard header (auto-detected from path if not specified)'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Set logging level (default: INFO)'
    )
    
    return parser.parse_args()


def auto_detect_audit_type(input_dir: str) -> str:
    """Auto-detect audit type from directory name."""
    folder_name = Path(input_dir).name.lower()
    
    for audit_type in AUDIT_CONFIGS:
        if audit_type in folder_name:
            return audit_type
    
    # Default to seo_audit if can't detect
    return 'seo_audit'


def auto_detect_website_name(input_dir: str) -> str:
    """Auto-detect website name from directory path."""
    path = Path(input_dir)
    
    # Try parent directory name
    parent = path.parent.name
    if parent and parent != '.' and not parent.startswith('output_'):
        return parent
    
    # Try grandparent
    grandparent = path.parent.parent.name
    if grandparent and grandparent != '.':
        return grandparent
    
    return 'Website Audit'


def main():
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    try:
        setup_logging(level=args.log_level)
    except Exception:
        pass
    
    # Auto-detect parameters if not provided
    audit_type = args.audit_type or auto_detect_audit_type(args.input_dir)
    website_name = args.website or auto_detect_website_name(args.input_dir)
    
    logger.info(f"Generating dashboard for {website_name}")
    logger.info(f"Audit type: {audit_type}")
    logger.info(f"Input directory: {args.input_dir}")
    
    # Load audit results
    try:
        results, metadata = load_audit_results(args.input_dir, audit_type)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1
    
    if not results:
        logger.warning("No audit results found in the input directory")
        return 1
    
    logger.info(f"Loaded {len(results)} audit results")
    
    # Generate output path
    if args.output:
        output_path = args.output
    else:
        safe_website = re.sub(r'[^\w\-.]', '_', website_name)
        output_path = f"{safe_website}_{audit_type}_dashboard.html"
    
    # Generate dashboard HTML
    html_content = generate_html_dashboard(
        results=results,
        metadata=metadata,
        website_name=website_name,
        output_path=output_path
    )
    
    # Write output file
    output_path = Path(output_path)
    output_path.write_text(html_content, encoding='utf-8')
    
    file_size = output_path.stat().st_size
    logger.info(f"Dashboard generated: {output_path} ({file_size / 1024:.1f} KB)")
    
    return 0


if __name__ == "__main__":
    exit(main())
