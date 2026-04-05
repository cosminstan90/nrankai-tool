"""
Historical tracking system for Website LLM Analyzer audit results.

Stores audit results over time and provides comparison functionality.

Author: Claude AI
Created: 2026-02-10
"""

import os
import json
import shutil
import hashlib
import re
from datetime import datetime
from typing import Optional

# Import logger
from core.logger import get_logger

# Initialize module logger
logger = get_logger(__name__)


# ============================================================================
# SCORE EXTRACTION CONFIGURATION
# ============================================================================

# Maps audit type to score extraction configuration
# Each entry defines how to extract the primary score from JSON and filename
AUDIT_SCORE_CONFIG = {
    'SEO_AUDIT': {
        'json_path': ['seo_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'GEO_AUDIT': {
        'json_path': ['geo_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'ACCESSIBILITY_AUDIT': {
        'json_path': ['accessibility_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'UX_CONTENT': {
        'json_path': ['ux_content_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'LEGAL_GDPR': {
        'json_path': ['gdpr_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'CONTENT_QUALITY': {
        'json_path': ['content_quality', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'BRAND_VOICE': {
        'json_path': ['brand_voice_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'E_COMMERCE': {
        'json_path': ['ecommerce_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'TRANSLATION_QUALITY': {
        'json_path': ['translation_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'COMPETITOR_ANALYSIS': {
        'json_path': ['competitive_positioning_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'INTERNAL_LINKING': {
        'json_path': ['internal_linking', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'SPELLING_GRAMMAR': {
        'json_path': ['spelling_grammar_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'READABILITY_AUDIT': {
        'json_path': ['readability_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'TECHNICAL_SEO': {
        'json_path': ['technical_seo_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'CONTENT_FRESHNESS': {
        'json_path': ['freshness_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 39, 'obsolete'), (40, 59, 'outdated'), (60, 79, 'mixed'), (80, 100, 'fresh')]
    },
    'LOCAL_SEO': {
        'json_path': ['local_seo_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'SECURITY_CONTENT_AUDIT': {
        'json_path': ['security_content_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'AI_OVERVIEW_OPTIMIZATION': {
        'json_path': ['ai_overview_audit', 'overall_score'],
        'prefix_digits': 3,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'RELEVANCY_AUDIT': {
        'json_path': ['relevancy_audit', 'score_current_probability'],
        'prefix_digits': 2,
        'score_range': (0, 100),
        'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
    },
    'KANTAR': {
        'json_path': None,  # Special handling - sum of meaningful + different
        'prefix_digits': 2,
        'score_range': (0, 20),
        'buckets': [(0, 5, 'poor'), (6, 12, 'needs_work'), (13, 16, 'good'), (17, 20, 'excellent')]
    },
    'GREENWASHING': {
        'json_path': None,  # Count-based
        'prefix_digits': 2,
        'score_range': (0, 999),
        'buckets': [(0, 0, 'excellent'), (1, 3, 'good'), (4, 6, 'needs_work'), (7, 999, 'poor')],
        'invert': True  # Lower is better
    },
    'ADVERTISMENT': {
        'json_path': None,  # Count-based
        'prefix_digits': 2,
        'score_range': (0, 999),
        'buckets': [(0, 0, 'excellent'), (1, 3, 'good'), (4, 6, 'needs_work'), (7, 999, 'poor')],
        'invert': True
    },
    'SPELLING_GRAMMAR': {
        'json_path': None,  # Count-based
        'prefix_digits': 2,
        'score_range': (0, 999),
        'buckets': [(0, 0, 'excellent'), (1, 5, 'good'), (6, 10, 'needs_work'), (11, 999, 'poor')],
        'invert': True
    },
}

# Default configuration for unknown audit types
DEFAULT_AUDIT_CONFIG = {
    'json_path': None,
    'prefix_digits': 3,
    'score_range': (0, 100),
    'buckets': [(0, 49, 'poor'), (50, 69, 'needs_work'), (70, 84, 'good'), (85, 100, 'excellent')]
}


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_file_hash(filepath: str) -> str:
    """Calculate MD5 hash of file content for deduplication."""
    with open(filepath, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def extract_score_from_filename(filename: str, prefix_digits: int = 3) -> Optional[int]:
    """
    Extract numerical score from filename prefix.
    
    Args:
        filename: Filename with digit prefix (e.g., "085_page.json")
        prefix_digits: Expected number of digits in prefix (2 or 3)
    
    Returns:
        Integer score or None if no match
    """
    pattern = re.compile(rf'^(\d{{{prefix_digits}}})')
    match = pattern.match(filename)
    if match:
        return int(match.group(1))
    
    # Fallback: try to match any 2-3 digit prefix
    fallback_pattern = re.compile(r'^(\d{2,3})')
    match = fallback_pattern.match(filename)
    return int(match.group(1)) if match else None


def extract_score_from_json(data: dict, json_path: list) -> Optional[int]:
    """
    Extract score from JSON data using a path.
    
    Args:
        data: JSON data dictionary
        json_path: List of keys to traverse
    
    Returns:
        Integer score or None if path doesn't exist
    """
    if not json_path:
        return None
    
    current = data
    for key in json_path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    
    try:
        return int(current)
    except (ValueError, TypeError):
        return None


def get_page_name_from_filename(filename: str) -> str:
    """
    Extract clean page name from result filename.
    
    Removes score prefix and .json extension.
    E.g., "085_homepage.json" -> "homepage.txt"
    """
    # Remove score prefix (2-3 digits followed by underscore)
    name = re.sub(r'^\d{2,3}_', '', filename)
    # Also handle prefixes with additional suffixes like "085_AA_"
    name = re.sub(r'^\d{2,3}_[A-Z]+_', '', name)
    # Replace .json with .txt to match original input files
    name = name.replace('.json', '.txt')
    return name


def get_score_bucket(score: int, buckets: list, invert: bool = False) -> str:
    """
    Determine which bucket a score falls into.
    
    Args:
        score: The score value
        buckets: List of (min, max, label) tuples
        invert: If True, interpret lower scores as better
    
    Returns:
        Bucket label string
    """
    for min_val, max_val, label in buckets:
        if min_val <= score <= max_val:
            return label
    return 'unknown'


def calculate_score_distribution(scores: list, buckets: list) -> dict:
    """
    Calculate distribution of scores across buckets.
    
    Args:
        scores: List of score values
        buckets: List of (min, max, label) tuples
    
    Returns:
        Dictionary mapping bucket labels to counts
    """
    distribution = {label: 0 for _, _, label in buckets}
    for score in scores:
        for min_val, max_val, label in buckets:
            if min_val <= score <= max_val:
                distribution[label] += 1
                break
    return distribution


# ============================================================================
# HISTORY DIRECTORY MANAGEMENT
# ============================================================================

def get_history_dir(website: str) -> str:
    """Get the history directory path for a website."""
    return os.path.join(website, 'history')


def get_runs_index_path(website: str) -> str:
    """Get the path to runs.json index file."""
    return os.path.join(get_history_dir(website), 'runs.json')


def get_run_dir(website: str, run_id: str) -> str:
    """Get the directory path for a specific run."""
    return os.path.join(get_history_dir(website), run_id)


def ensure_history_dir(website: str) -> str:
    """Create history directory if it doesn't exist."""
    history_dir = get_history_dir(website)
    os.makedirs(history_dir, exist_ok=True)
    return history_dir


def load_runs_index(website: str) -> dict:
    """
    Load the runs.json index file.
    
    Returns empty structure if file doesn't exist.
    """
    runs_path = get_runs_index_path(website)
    if os.path.exists(runs_path):
        try:
            with open(runs_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load runs index: {e}")
    
    return {
        'website': website,
        'runs': []
    }


def save_runs_index(website: str, index_data: dict) -> None:
    """Save the runs.json index file."""
    ensure_history_dir(website)
    runs_path = get_runs_index_path(website)
    with open(runs_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, indent=2)


# ============================================================================
# CORE HISTORY FUNCTIONS
# ============================================================================

def archive_current_run(
    website: str,
    audit_type: str,
    output_dir: Optional[str] = None,
    provider: str = "UNKNOWN",
    model: str = "unknown"
) -> Optional[dict]:
    """
    Archive current audit results to history.
    
    Copies all results from the output directory to a timestamped folder
    in history/, updates runs.json index, and creates run_meta.json.
    
    Args:
        website: Website domain name
        audit_type: Type of audit (e.g., "SEO_AUDIT")
        output_dir: Path to output directory (auto-detected if None)
        provider: LLM provider name
        model: Model name used
    
    Returns:
        Run metadata dictionary, or None if archiving failed
    """
    # Auto-detect output directory if not provided
    if output_dir is None:
        output_dir = os.path.join(website, f"output_{audit_type.lower()}")
    
    if not os.path.exists(output_dir) or not os.path.isdir(output_dir):
        logger.warning(f"Output directory not found: {output_dir}")
        return None
    
    # List result files
    result_files = [f for f in os.listdir(output_dir) if f.endswith('.json')]
    if not result_files:
        logger.warning(f"No JSON files found in {output_dir}")
        return None
    
    # Generate run ID and timestamp
    timestamp = datetime.utcnow()
    run_id = f"{timestamp.strftime('%Y-%m-%d')}_{audit_type.lower()}"
    
    # Check if run already exists today, append counter if needed
    history_dir = get_history_dir(website)
    counter = 1
    original_run_id = run_id
    while os.path.exists(os.path.join(history_dir, run_id)):
        counter += 1
        run_id = f"{original_run_id}_{counter}"
    
    # Create run directory
    run_dir = get_run_dir(website, run_id)
    results_dir = os.path.join(run_dir, 'results')
    os.makedirs(results_dir, exist_ok=True)
    
    # Get audit configuration
    config = AUDIT_SCORE_CONFIG.get(audit_type.upper(), DEFAULT_AUDIT_CONFIG)
    buckets = config['buckets']
    prefix_digits = config['prefix_digits']
    
    # Copy result files and extract scores
    per_page_scores = {}
    scores = []
    content_hashes = {}
    
    for filename in result_files:
        src_path = os.path.join(output_dir, filename)
        dst_path = os.path.join(results_dir, filename)
        
        # Extract score from filename
        score = extract_score_from_filename(filename, prefix_digits)
        
        # Also try to extract from JSON content
        try:
            with open(src_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if config['json_path'] and score is None:
                    score = extract_score_from_json(data, config['json_path'])
        except (json.JSONDecodeError, IOError):
            pass
        
        # Store score by page name
        page_name = get_page_name_from_filename(filename)
        if score is not None:
            per_page_scores[page_name] = score
            scores.append(score)
        
        # Calculate content hash for deduplication
        content_hash = get_file_hash(src_path)
        content_hashes[filename] = content_hash
        
        # Copy file
        shutil.copy2(src_path, dst_path)
    
    # Calculate statistics
    total_pages = len(result_files)
    average_score = round(sum(scores) / len(scores), 1) if scores else 0
    median_score = sorted(scores)[len(scores) // 2] if scores else 0
    score_distribution = calculate_score_distribution(scores, buckets)
    
    # Load previous run for comparison
    index = load_runs_index(website)
    previous_runs = [r for r in index['runs'] if r['audit_type'] == audit_type.upper()]
    
    pages_improved = 0
    pages_degraded = 0
    pages_new = 0
    pages_removed = 0
    
    if previous_runs:
        # Compare with most recent run of same audit type
        prev_run = previous_runs[-1]
        prev_run_dir = get_run_dir(website, prev_run['run_id'])
        prev_meta_path = os.path.join(prev_run_dir, 'run_meta.json')
        
        if os.path.exists(prev_meta_path):
            with open(prev_meta_path, 'r', encoding='utf-8') as f:
                prev_meta = json.load(f)
                prev_scores = prev_meta.get('per_page_scores', {})
                
                current_pages = set(per_page_scores.keys())
                previous_pages = set(prev_scores.keys())
                
                # New pages
                pages_new = len(current_pages - previous_pages)
                
                # Removed pages
                pages_removed = len(previous_pages - current_pages)
                
                # Compare overlapping pages
                for page in current_pages & previous_pages:
                    curr_score = per_page_scores[page]
                    prev_score = prev_scores[page]
                    if curr_score > prev_score:
                        pages_improved += 1
                    elif curr_score < prev_score:
                        pages_degraded += 1
    else:
        # First run - all pages are "new"
        pages_new = total_pages
    
    # Create run metadata
    run_meta = {
        'run_id': run_id,
        'timestamp': timestamp.isoformat() + 'Z',
        'audit_type': audit_type.upper(),
        'provider': provider,
        'model': model,
        'total_pages': total_pages,
        'pages_improved': pages_improved,
        'pages_degraded': pages_degraded,
        'pages_new': pages_new,
        'pages_removed': pages_removed,
        'average_score': average_score,
        'median_score': median_score,
        'score_distribution': score_distribution,
        'per_page_scores': per_page_scores,
        'content_hashes': content_hashes,
    }
    
    # Save run_meta.json
    meta_path = os.path.join(run_dir, 'run_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(run_meta, f, indent=2)
    
    # Update runs index
    index_entry = {
        'run_id': run_id,
        'timestamp': run_meta['timestamp'],
        'audit_type': audit_type.upper(),
        'provider': provider,
        'model': model,
        'total_pages': total_pages,
        'average_score': average_score,
        'score_distribution': score_distribution,
    }
    
    index['runs'].append(index_entry)
    save_runs_index(website, index)
    
    logger.info(
        f"Archived run {run_id}: {total_pages} pages, "
        f"avg score {average_score}, "
        f"+{pages_improved}/-{pages_degraded} vs previous"
    )
    
    return run_meta


def get_run_history(website: str, audit_type: Optional[str] = None) -> list:
    """
    Get all historical runs for a website.
    
    Args:
        website: Website domain name
        audit_type: Filter by audit type (optional)
    
    Returns:
        List of run metadata dictionaries, sorted by timestamp
    """
    index = load_runs_index(website)
    runs = index.get('runs', [])
    
    if audit_type:
        runs = [r for r in runs if r['audit_type'] == audit_type.upper()]
    
    # Sort by timestamp
    runs.sort(key=lambda r: r['timestamp'])
    
    return runs


def get_run_metadata(website: str, run_id: str) -> Optional[dict]:
    """
    Load full metadata for a specific run.
    
    Args:
        website: Website domain name
        run_id: Run identifier
    
    Returns:
        Full run metadata dictionary or None if not found
    """
    meta_path = os.path.join(get_run_dir(website, run_id), 'run_meta.json')
    
    if not os.path.exists(meta_path):
        logger.warning(f"Run metadata not found: {meta_path}")
        return None
    
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to load run metadata: {e}")
        return None


def compare_runs(
    website: str,
    run_id_old: str,
    run_id_new: str
) -> Optional[dict]:
    """
    Generate detailed page-by-page comparison between two runs.
    
    Args:
        website: Website domain name
        run_id_old: Earlier run identifier
        run_id_new: Later run identifier
    
    Returns:
        Comparison dictionary with all metrics, or None if runs not found
    """
    old_meta = get_run_metadata(website, run_id_old)
    new_meta = get_run_metadata(website, run_id_new)
    
    if not old_meta or not new_meta:
        return None
    
    old_scores = old_meta.get('per_page_scores', {})
    new_scores = new_meta.get('per_page_scores', {})
    
    old_pages = set(old_scores.keys())
    new_pages = set(new_scores.keys())
    
    # Pages in both runs
    common_pages = old_pages & new_pages
    
    # Calculate changes
    improved = []
    degraded = []
    unchanged = []
    
    for page in common_pages:
        old_score = old_scores[page]
        new_score = new_scores[page]
        change = new_score - old_score
        
        entry = {
            'page': page,
            'old_score': old_score,
            'new_score': new_score,
            'change': change
        }
        
        if change > 0:
            improved.append(entry)
        elif change < 0:
            degraded.append(entry)
        else:
            unchanged.append(entry)
    
    # Sort by magnitude of change
    improved.sort(key=lambda x: x['change'], reverse=True)
    degraded.sort(key=lambda x: x['change'])
    
    # New and removed pages
    new_page_list = [
        {'page': p, 'score': new_scores[p]}
        for p in (new_pages - old_pages)
    ]
    new_page_list.sort(key=lambda x: x['score'], reverse=True)
    
    removed_page_list = [
        {'page': p, 'score': old_scores[p]}
        for p in (old_pages - new_pages)
    ]
    removed_page_list.sort(key=lambda x: x['score'], reverse=True)
    
    # Overall comparison
    comparison = {
        'website': website,
        'old_run': {
            'run_id': run_id_old,
            'timestamp': old_meta['timestamp'],
            'total_pages': old_meta['total_pages'],
            'average_score': old_meta['average_score'],
            'score_distribution': old_meta['score_distribution'],
        },
        'new_run': {
            'run_id': run_id_new,
            'timestamp': new_meta['timestamp'],
            'total_pages': new_meta['total_pages'],
            'average_score': new_meta['average_score'],
            'score_distribution': new_meta['score_distribution'],
        },
        'score_change': round(new_meta['average_score'] - old_meta['average_score'], 1),
        'pages_improved': improved,
        'pages_degraded': degraded,
        'pages_unchanged': len(unchanged),
        'pages_new': new_page_list,
        'pages_removed': removed_page_list,
        'audit_type': new_meta['audit_type'],
    }
    
    return comparison


def get_trend(
    website: str,
    audit_type: str,
    metric: str = "average_score"
) -> list:
    """
    Get time series data for a specific metric.
    
    Args:
        website: Website domain name
        audit_type: Type of audit
        metric: Metric to track (default: "average_score")
    
    Returns:
        List of (timestamp, value) tuples, sorted chronologically
    """
    runs = get_run_history(website, audit_type)
    
    trend_data = []
    for run in runs:
        timestamp = run['timestamp']
        
        # Handle different metric types
        if metric == 'average_score':
            value = run.get('average_score', 0)
        elif metric == 'total_pages':
            value = run.get('total_pages', 0)
        elif metric.startswith('distribution_'):
            bucket = metric.replace('distribution_', '')
            value = run.get('score_distribution', {}).get(bucket, 0)
        else:
            value = run.get(metric, 0)
        
        trend_data.append((timestamp, value))
    
    return trend_data


def get_latest_run(website: str, audit_type: str) -> Optional[dict]:
    """
    Get the most recent run for a website and audit type.
    
    Returns:
        Run metadata dictionary or None if no runs exist
    """
    runs = get_run_history(website, audit_type)
    return runs[-1] if runs else None


def get_previous_run(website: str, audit_type: str) -> Optional[dict]:
    """
    Get the second most recent run for a website and audit type.
    
    Returns:
        Run metadata dictionary or None if fewer than 2 runs exist
    """
    runs = get_run_history(website, audit_type)
    return runs[-2] if len(runs) >= 2 else None


# ============================================================================
# INTEGRATION HELPER
# ============================================================================

def auto_archive_and_compare(
    website: str,
    audit_type: str,
    output_dir: Optional[str] = None,
    provider: str = "UNKNOWN",
    model: str = "unknown",
    print_summary: bool = True
) -> Optional[dict]:
    """
    Archive current run and print comparison summary if previous run exists.
    
    This is the main integration point for monitor_completion_LLM_batch.py.
    
    Args:
        website: Website domain name
        audit_type: Type of audit
        output_dir: Path to output directory
        provider: LLM provider name
        model: Model name used
        print_summary: Whether to print comparison summary
    
    Returns:
        Run metadata dictionary
    """
    # Get previous run before archiving
    prev_run = get_latest_run(website, audit_type)
    
    # Archive current run
    run_meta = archive_current_run(
        website=website,
        audit_type=audit_type,
        output_dir=output_dir,
        provider=provider,
        model=model
    )
    
    if not run_meta:
        return None
    
    # Print comparison if previous run exists
    if print_summary and prev_run:
        comparison = compare_runs(
            website=website,
            run_id_old=prev_run['run_id'],
            run_id_new=run_meta['run_id']
        )
        if comparison:
            print_comparison_summary(comparison)
    elif print_summary:
        # First run - just print summary
        print_first_run_summary(run_meta)
    
    return run_meta


def print_first_run_summary(run_meta: dict) -> None:
    """Print summary for first run (no comparison available)."""
    print("\n" + "═" * 50)
    print(f"  {run_meta['audit_type']} BASELINE ESTABLISHED")
    print("═" * 50)
    print(f"Run ID:         {run_meta['run_id']}")
    print(f"Pages Analyzed: {run_meta['total_pages']}")
    print(f"Average Score:  {run_meta['average_score']}")
    print(f"Median Score:   {run_meta['median_score']}")
    print("\nScore Distribution:")
    for bucket, count in run_meta['score_distribution'].items():
        print(f"  {bucket.replace('_', ' ').title():15} {count:>4}")
    print("═" * 50)
    print("Historical tracking enabled. Future runs will show comparisons.\n")


def print_comparison_summary(comparison: dict, top_n: int = 5) -> None:
    """
    Print formatted comparison summary to console.
    
    Args:
        comparison: Comparison dictionary from compare_runs()
        top_n: Number of top improvers/decliners to show
    """
    old = comparison['old_run']
    new = comparison['new_run']
    change = comparison['score_change']
    
    # Direction indicator
    if change > 0:
        direction = "▲"
        change_str = f"+{change}"
    elif change < 0:
        direction = "▼"
        change_str = str(change)
    else:
        direction = "─"
        change_str = "0"
    
    print("\n" + "═" * 54)
    print(f"  {comparison['audit_type']} COMPARISON: {comparison['website']}")
    print(f"  Run 1: {old['timestamp'][:10]}  →  Run 2: {new['timestamp'][:10]}")
    print("═" * 54)
    
    # Overall score
    print(f"\nOverall Score:  {old['average_score']} → {new['average_score']}  ({change_str}) {direction}")
    
    # Pages count
    old_pages = old['total_pages']
    new_pages = new['total_pages']
    page_diff = new_pages - old_pages
    page_change = f"+{page_diff}" if page_diff >= 0 else str(page_diff)
    print(f"Pages Analyzed: {old_pages} → {new_pages}   ({page_change} pages)")
    
    # Score distribution
    print("\nScore Distribution:")
    old_dist = old['score_distribution']
    new_dist = new['score_distribution']
    
    # Standard bucket order for display
    bucket_order = ['excellent', 'good', 'needs_work', 'poor']
    bucket_labels = {
        'excellent': 'Excellent (85-100)',
        'good': 'Good (70-84)',
        'needs_work': 'Needs Work (50-69)',
        'poor': 'Poor (0-49)'
    }
    
    for bucket in bucket_order:
        if bucket in old_dist or bucket in new_dist:
            old_val = old_dist.get(bucket, 0)
            new_val = new_dist.get(bucket, 0)
            diff = new_val - old_val
            
            if diff > 0:
                diff_str = f"+{diff}"
                ind = "▲" if bucket in ['excellent', 'good'] else "▼"
            elif diff < 0:
                diff_str = str(diff)
                ind = "▼" if bucket in ['excellent', 'good'] else "▲"
            else:
                diff_str = "0"
                ind = " "
            
            label = bucket_labels.get(bucket, bucket.replace('_', ' ').title())
            print(f"  {label:20} {old_val:>3} → {new_val:>3}  ({diff_str:>3})  {ind}")
    
    # Top improvers
    improved = comparison['pages_improved']
    if improved:
        print(f"\nTop Improvers:")
        for entry in improved[:top_n]:
            marker = "★" if entry['change'] >= 20 else " "
            print(f"  {entry['page']:30} {entry['old_score']:>3} → {entry['new_score']:>3}  (+{entry['change']}) {marker}")
    
    # Top decliners
    degraded = comparison['pages_degraded']
    if degraded:
        print(f"\nTop Decliners:")
        for entry in degraded[:top_n]:
            marker = "⚠" if entry['change'] <= -10 else " "
            print(f"  {entry['page']:30} {entry['old_score']:>3} → {entry['new_score']:>3}  ({entry['change']}) {marker}")
    
    # New pages
    new_pages_list = comparison['pages_new']
    if new_pages_list:
        print(f"\nNew Pages (not in previous run):")
        for entry in new_pages_list[:top_n]:
            print(f"  {entry['page']:30} {entry['score']:>3}")
        if len(new_pages_list) > top_n:
            print(f"  ... and {len(new_pages_list) - top_n} more")
    
    # Removed pages
    removed_pages = comparison['pages_removed']
    if removed_pages:
        print(f"\nRemoved Pages (in previous but not current):")
        for entry in removed_pages[:top_n]:
            print(f"  {entry['page']:30} (was {entry['score']})")
        if len(removed_pages) > top_n:
            print(f"  ... and {len(removed_pages) - top_n} more")
    
    print("\n" + "═" * 54 + "\n")


if __name__ == "__main__":
    # Example usage
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python history_tracker.py <website> <audit_type>")
        print("Example: python history_tracker.py example.com SEO_AUDIT")
        sys.exit(1)
    
    website = sys.argv[1]
    audit_type = sys.argv[2]
    
    # Archive current run
    run_meta = auto_archive_and_compare(
        website=website,
        audit_type=audit_type,
        provider="CLI",
        model="manual"
    )
    
    if run_meta:
        print(f"\nSuccessfully archived run: {run_meta['run_id']}")
    else:
        print("\nFailed to archive run")
