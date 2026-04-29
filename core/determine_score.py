"""
Aggregates audit results into Excel report with score buckets.

Supports CLI arguments to customize scan directory and output file.
Dynamically loads bucket configuration from custom audit YAML files.

Author: Cosmin
Created: 2026-01-23
Updated: 2026-02-10 - Added proper logging and error handling
Updated: 2026-02-12 - Added support for custom audit bucket configurations
"""

import os
import re
import pandas as pd
import json
import argparse

# Import logger
from core.logger import get_logger, setup_logging

# Initialize module logger
logger = get_logger(__name__)

# Import audit_builder for custom audit bucket configurations
try:
    from core.audit_builder import (
        get_registry,
        get_score_buckets as get_custom_buckets
    )
    from core.prompt_loader import is_custom_audit, get_audit_definition
    AUDIT_BUILDER_AVAILABLE = True
except ImportError:
    AUDIT_BUILDER_AVAILABLE = False


def extract_score(filename, pattern):
    """
    Extract numerical score from filename prefix.
    
    Args:
        filename: Filename with 2-3 digit prefix (e.g., "85_page.json")
        pattern: Compiled regex pattern
    
    Returns:
        Integer score or None if no match
    """
    match = pattern.match(filename)
    return int(match.group(1)) if match else None


def extract_json_summary(filepath):
    """
    Extract summary data from JSON audit result file.
    
    Args:
        filepath: Path to JSON result file
    
    Returns:
        Dictionary with audit data
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load JSON from {filepath}: {e}")
        return {}


def perform_full_audit_suite(root_dir='.',output_filename='audit_scores.xlsx'):
    """
    Scan directories for audit outputs and aggregate into Excel report.
    
    Dynamically discovers custom audits and their bucket configurations.
    Also includes cross-reference analysis findings if available.
    
    Args:
        root_dir: Root directory to scan for audit folders
        output_filename: Output Excel filename
    """
    # Initialize data containers for each audit domain
    # Start with built-in audits
    audit_data = {
        'relevancy_audit': [],
        'spelling_grammar': [],
        'greenwashing': [],
        'advertisment': [],
        'kantar': [],
        'seo_audit': [],
        'geo_audit': [],
        'accessibility_audit': [],
        'ux_content': [],
        'legal_gdpr': [],
        'content_quality': [],
        'brand_voice': [],
        'e_commerce': [],
        'translation_quality': [],
        'competitor_analysis': [],
        'internal_linking': [],
        'readability_audit': [],
        'technical_seo': [],
        'content_freshness': [],
        'local_seo': [],
        'security_content_audit': [],
        'ai_overview_optimization': [],
    }

    # Mapping of folder names to data keys
    folder_mapping = {
        'output_relevancy_audit': 'relevancy_audit',
        'output_spelling_grammar': 'spelling_grammar',
        'output_greenwashing': 'greenwashing',
        'output_advertisment': 'advertisment',
        'output_kantar': 'kantar',
        'output_seo_audit': 'seo_audit',
        'output_geo_audit': 'geo_audit',
        'output_accessibility_audit': 'accessibility_audit',
        'output_ux_content': 'ux_content',
        'output_legal_gdpr': 'legal_gdpr',
        'output_content_quality': 'content_quality',
        'output_brand_voice': 'brand_voice',
        'output_e_commerce': 'e_commerce',
        'output_translation_quality': 'translation_quality',
        'output_competitor_analysis': 'competitor_analysis',
        'output_internal_linking': 'internal_linking',
        'output_readability_audit': 'readability_audit',
        'output_technical_seo': 'technical_seo',
        'output_content_freshness': 'content_freshness',
        'output_local_seo': 'local_seo',
        'output_security_content_audit': 'security_content_audit',
        'output_ai_overview_optimization': 'ai_overview_optimization',
    }

    # Score buckets configuration for different audit types
    _default_buckets = [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    score_config = {
        'relevancy_audit': {'buckets': [(0, 49, 'obsolete'), (50, 89, 'needs_update'), (90, 100, 'evergreen')]},
        'spelling_grammar': {'buckets': [(0, 0, 'perfect'), (1, 5, 'minor'), (6, 10, 'moderate'), (11, 999, 'major')]},
        'greenwashing': {'buckets': [(0, 0, 'compliant'), (1, 3, 'minor_issues'), (4, 999, 'major_issues')]},
        'advertisment': {'buckets': [(0, 0, 'compliant'), (1, 3, 'minor_issues'), (4, 999, 'major_issues')]},
        'kantar': {'buckets': [(0, 5, 'low'), (6, 12, 'medium'), (13, 20, 'high')]},
        'seo_audit': {'buckets': _default_buckets},
        'geo_audit': {'buckets': [(0, 49, 'low_citation'), (50, 69, 'moderate'), (70, 84, 'good'), (85, 100, 'high_citation')]},
        'accessibility_audit': {'buckets': [(0, 49, 'non_compliant'), (50, 69, 'partial'), (70, 84, 'mostly_compliant'), (85, 100, 'compliant')]},
        'ux_content': {'buckets': _default_buckets},
        'legal_gdpr': {'buckets': [(0, 49, 'high_risk'), (50, 69, 'medium_risk'), (70, 84, 'low_risk'), (85, 100, 'compliant')]},
        'content_quality': {'buckets': [(0, 49, 'thin'), (50, 69, 'standard'), (70, 84, 'good'), (85, 100, 'high_quality')]},
        'brand_voice': {'buckets': [(0, 49, 'inconsistent'), (50, 69, 'moderate'), (70, 84, 'consistent'), (85, 100, 'excellent')]},
        'e_commerce': {'buckets': [(0, 49, 'low_conversion'), (50, 69, 'moderate'), (70, 84, 'good'), (85, 100, 'optimized')]},
        'translation_quality': {'buckets': _default_buckets},
        'competitor_analysis': {'buckets': [(0, 49, 'lagging'), (50, 69, 'average'), (70, 84, 'competitive'), (85, 100, 'leader')]},
        'internal_linking': {'buckets': _default_buckets},
        'readability_audit': {'buckets': _default_buckets},
        'technical_seo': {'buckets': _default_buckets},
        'content_freshness': {'buckets': [(0, 39, 'obsolete'), (40, 59, 'outdated'), (60, 79, 'mixed'), (80, 100, 'fresh')]},
        'local_seo': {'buckets': _default_buckets},
        'security_content_audit': {'buckets': [(0, 49, 'high_risk'), (50, 69, 'medium_risk'), (70, 84, 'low_risk'), (85, 100, 'secure')]},
        'ai_overview_optimization': {'buckets': _default_buckets},
    }

    # Sheet name mapping for built-in audits
    sheet_names = {
        'seo_audit': 'SEO Audit',
        'geo_audit': 'GEO Audit',
        'content_quality': 'Content Quality',
        'accessibility_audit': 'Accessibility',
        'ux_content': 'UX Content',
        'legal_gdpr': 'Legal GDPR',
        'brand_voice': 'Brand Voice',
        'e_commerce': 'E-Commerce',
        'translation_quality': 'Translation',
        'competitor_analysis': 'Competitor Analysis',
        'relevancy_audit': 'Relevancy Audit',
        'spelling_grammar': 'Spelling & Grammar',
        'greenwashing': 'Greenwashing',
        'advertisment': 'Advertisment',
        'kantar': 'Kantar MDS',
        'internal_linking': 'Internal Linking',
        'readability_audit': 'Readability',
        'technical_seo': 'Technical SEO',
        'content_freshness': 'Content Freshness',
        'local_seo': 'Local SEO',
        'security_content_audit': 'Security Content',
        'ai_overview_optimization': 'AI Overview',
    }

    # Discover custom audits and add their configurations
    if AUDIT_BUILDER_AVAILABLE:
        try:
            registry = get_registry()
            custom_audits = registry.list_custom_audits()
            
            for audit_info in custom_audits:
                audit_type = audit_info['type'].lower()
                folder_name = f'output_{audit_type}'
                
                # Skip if already in built-in audits
                if audit_type in audit_data:
                    continue
                
                # Add to data containers
                audit_data[audit_type] = []
                folder_mapping[folder_name] = audit_type
                
                # Get bucket configuration from audit definition
                definition = registry.get_definition(audit_info['type'])
                if definition:
                    buckets = get_custom_buckets(definition)
                    score_config[audit_type] = {'buckets': buckets}
                    sheet_names[audit_type] = audit_info['name']
                    logger.debug(f"Added custom audit: {audit_type}")
                else:
                    score_config[audit_type] = {'buckets': [(0, 100, 'default')]}
                    sheet_names[audit_type] = audit_info.get('name', audit_type.replace('_', ' ').title())
                    
        except Exception as e:
            logger.warning(f"Error loading custom audits: {e}")

    # Regex to capture 2 or 3 digits at the start of the filename
    prefix_pattern = re.compile(r'^(\d{2,3})')

    logger.info(f"Scanning directory: {root_dir}")
    
    total_files_found = 0
    
    # Container for cross-reference analysis data
    crossref_data = []

    # Iterate through subdirectories
    for subdir in os.listdir(root_dir):
        subdir_path = os.path.join(root_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue

        # Check each possible output folder
        for folder_name, data_key in folder_mapping.items():
            output_path = os.path.join(subdir_path, folder_name)
            if os.path.exists(output_path) and os.path.isdir(output_path):
                config = score_config.get(data_key, {'buckets': [(0, 100, 'default')]})
                
                # Initialize counts based on buckets
                counts = {'site': subdir, 'total_files': 0}
                for bucket in config['buckets']:
                    counts[bucket[2]] = 0

                for f in os.listdir(output_path):
                    if not f.endswith('.json'):
                        continue
                    
                    # Skip cross-reference files for bucket counts
                    if f.startswith('CROSS_REFERENCE'):
                        continue
                    
                    score = extract_score(f, prefix_pattern)
                    counts['total_files'] += 1
                    
                    if score is not None:
                        for min_val, max_val, label in config['buckets']:
                            if min_val <= score <= max_val:
                                counts[label] += 1
                                break

                if counts['total_files'] > 0:
                    audit_data[data_key].append(counts)
                    logger.info(f"Found {counts['total_files']} files in {subdir}/{folder_name}")
                    total_files_found += counts['total_files']
                
                # Check for cross-reference analysis file
                crossref_file = os.path.join(output_path, 'CROSS_REFERENCE_ANALYSIS.json')
                if os.path.exists(crossref_file):
                    crossref_summary = _load_crossref_summary(crossref_file, subdir, data_key)
                    if crossref_summary:
                        crossref_data.append(crossref_summary)

    # Excel Export Process
    logger.info(f"Generating Excel report: {output_filename}")

    # Define sheet configurations
    sheets_config = [
        (audit_data['seo_audit'], 'SEO Audit'),
        (audit_data['geo_audit'], 'GEO Audit'),
        (audit_data['content_quality'], 'Content Quality'),
        (audit_data['accessibility_audit'], 'Accessibility'),
        (audit_data['ux_content'], 'UX Content'),
        (audit_data['legal_gdpr'], 'Legal GDPR'),
        (audit_data['brand_voice'], 'Brand Voice'),
        (audit_data['e_commerce'], 'E-Commerce'),
        (audit_data['translation_quality'], 'Translation'),
        (audit_data['competitor_analysis'], 'Competitor Analysis'),
        (audit_data['relevancy_audit'], 'Relevancy Audit'),
        (audit_data['spelling_grammar'], 'Spelling & Grammar'),
        (audit_data['greenwashing'], 'Greenwashing'),
        (audit_data['advertisment'], 'Advertisment'),
        (audit_data['kantar'], 'Kantar MDS')
    ]

    sheets_created = 0
    try:
        with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
            for data_list, sheet_name in sheets_config:
                if data_list:  # Only create sheet if there's data
                    df = pd.DataFrame(data_list)
                    if not df.empty:
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
                        sheets_created += 1
                        logger.debug(f"Created sheet: {sheet_name}")
            
            # Add cross-reference analysis sheet if data exists
            if crossref_data:
                df_crossref = pd.DataFrame(crossref_data)
                if not df_crossref.empty:
                    df_crossref.to_excel(writer, sheet_name='Cross-Reference', index=False)
                    sheets_created += 1
                    logger.debug("Created sheet: Cross-Reference")
                    
                    # Also create detailed findings sheets if available
                    _create_crossref_detail_sheets(writer, crossref_data)

        if sheets_created > 0:
            # Summary log
            logger.info(
                f"Report generated: {output_filename} "
                f"({sheets_created} sheets, {total_files_found} total entries)"
            )
        else:
            logger.warning(f"No audit data found in {root_dir}")
            logger.info("Make sure audit output directories exist (e.g., output_seo_audit/)")
    
    except Exception as e:
        logger.error(f"Failed to generate Excel report: {e}", exc_info=True)


def _load_crossref_summary(filepath, site, audit_type):
    """
    Load and summarize cross-reference analysis for Excel.
    
    Args:
        filepath: Path to CROSS_REFERENCE_ANALYSIS.json
        site: Site name
        audit_type: Audit type
    
    Returns:
        Dictionary with summary data for Excel
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        analysis = data.get('cross_reference_analysis', {})
        stats = analysis.get('statistics', {})
        rule_findings = analysis.get('rule_based_findings', {})
        llm_analysis = analysis.get('llm_analysis', {})
        
        summary = {
            'site': site,
            'audit_type': audit_type.upper(),
            'pages_analyzed': analysis.get('pages_analyzed', 0),
            'analysis_date': analysis.get('analysis_date', ''),
            'avg_score': stats.get('average_score', 0),
            'median_score': stats.get('median_score', 0),
            'std_deviation': stats.get('std_deviation', 0),
            'min_score': stats.get('min_score', 0),
            'max_score': stats.get('max_score', 0),
            'keyword_cannibalization_count': len(rule_findings.get('keyword_cannibalization', [])),
            'duplicate_h1_count': len(rule_findings.get('duplicate_h1s', [])),
            'repeated_error_count': len(rule_findings.get('repeated_errors', [])),
            'thin_content_count': len(rule_findings.get('thin_content_pages', [])),
            'score_outlier_count': len(rule_findings.get('score_outliers', [])),
            'site_wide_issues_count': len(llm_analysis.get('site_wide_issues', [])),
            'content_gaps_count': len(llm_analysis.get('content_gaps', [])),
            'quick_wins_count': len(llm_analysis.get('quick_wins', []))
        }
        
        return summary
        
    except Exception as e:
        logger.warning(f"Failed to load cross-reference summary from {filepath}: {e}")
        return None


def _create_crossref_detail_sheets(writer, crossref_data):
    """
    Create detailed cross-reference finding sheets.
    
    Args:
        writer: Excel writer object
        crossref_data: List of cross-reference summaries
    """
    # Collect all keyword cannibalization issues
    all_keyword_issues = []
    all_site_issues = []
    
    for site_data in crossref_data:
        site = site_data.get('site', 'unknown')
        # We need to reload the full JSON to get detailed findings
        # This is done lazily - only if there are issues to report
    
    # For now, just log that detailed sheets could be created
    logger.debug("Detailed cross-reference sheets can be extended in future versions")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Aggregate audit results into Excel report',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Scan current directory
  python determine_score.py
  
  # Scan specific directory
  python determine_score.py --root-dir ./example.com
  
  # Custom output filename
  python determine_score.py --output my_report.xlsx
  
  # Combine both
  python determine_score.py --root-dir ./data --output results.xlsx
        '''
    )
    
    parser.add_argument(
        '--root-dir',
        type=str,
        default='.',
        help='Root directory to scan for audit folders (default: current directory)'
    )
    
    parser.add_argument(
        '--output',
        type=str,
        default='audit_scores.xlsx',
        help='Output Excel filename (default: audit_scores.xlsx)'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Set logging level (default: INFO)'
    )
    
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    
    # Setup logging with specified level
    setup_logging(level=args.log_level)
    
    perform_full_audit_suite(args.root_dir, args.output)
