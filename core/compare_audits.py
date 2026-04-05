#!/usr/bin/env python3
"""
CLI tool for comparing Website LLM Analyzer audit runs.

Provides commands to view history, compare runs, and track trends.

Author: Claude AI
Created: 2026-02-10

Usage:
    # Compare last two runs
    python compare_audits.py --website example.com --audit SEO_AUDIT
    
    # Compare specific runs
    python compare_audits.py --run-old 2026-01-15_seo_audit --run-new 2026-02-01_seo_audit
    
    # Show full history
    python compare_audits.py --website example.com --audit SEO_AUDIT --history
    
    # Show trend data
    python compare_audits.py --website example.com --audit SEO_AUDIT --trend
    
    # Export comparison to JSON
    python compare_audits.py --website example.com --audit SEO_AUDIT --export comparison.json
"""

import argparse
import json
import sys
from datetime import datetime

from core.history_tracker import (
    get_run_history,
    get_run_metadata,
    compare_runs,
    get_trend,
    get_latest_run,
    get_previous_run,
    print_comparison_summary,
    AUDIT_SCORE_CONFIG,
)

# Import logger
from core.logger import get_logger, setup_logging

logger = get_logger(__name__)


# ============================================================================
# DISPLAY FUNCTIONS
# ============================================================================

def display_history(website: str, audit_type: str = None) -> None:
    """Display all historical runs for a website."""
    runs = get_run_history(website, audit_type)
    
    if not runs:
        print(f"\nNo historical runs found for {website}")
        if audit_type:
            print(f"Audit type filter: {audit_type}")
        print("\nRun an audit first to start tracking history.")
        return
    
    print("\n" + "═" * 70)
    print(f"  AUDIT HISTORY: {website}")
    if audit_type:
        print(f"  Filter: {audit_type}")
    print("═" * 70)
    print(f"\n{'Run ID':<35} {'Date':<12} {'Type':<15} {'Pages':>6} {'Avg':>6}")
    print("-" * 70)
    
    for run in runs:
        run_id = run['run_id']
        date = run['timestamp'][:10]
        audit = run['audit_type']
        pages = run['total_pages']
        avg = run['average_score']
        
        print(f"{run_id:<35} {date:<12} {audit:<15} {pages:>6} {avg:>6.1f}")
    
    print("-" * 70)
    print(f"Total runs: {len(runs)}")
    print("═" * 70 + "\n")


def display_run_details(website: str, run_id: str) -> None:
    """Display detailed information about a specific run."""
    meta = get_run_metadata(website, run_id)
    
    if not meta:
        print(f"\nRun not found: {run_id}")
        return
    
    print("\n" + "═" * 60)
    print(f"  RUN DETAILS: {run_id}")
    print("═" * 60)
    
    print(f"\nTimestamp:      {meta['timestamp']}")
    print(f"Audit Type:     {meta['audit_type']}")
    print(f"Provider:       {meta['provider']}")
    print(f"Model:          {meta['model']}")
    print(f"\nTotal Pages:    {meta['total_pages']}")
    print(f"Average Score:  {meta['average_score']}")
    print(f"Median Score:   {meta['median_score']}")
    
    print(f"\nChanges from Previous Run:")
    print(f"  Pages Improved:  {meta.get('pages_improved', 0)}")
    print(f"  Pages Degraded:  {meta.get('pages_degraded', 0)}")
    print(f"  New Pages:       {meta.get('pages_new', 0)}")
    print(f"  Removed Pages:   {meta.get('pages_removed', 0)}")
    
    print(f"\nScore Distribution:")
    for bucket, count in meta['score_distribution'].items():
        pct = (count / meta['total_pages'] * 100) if meta['total_pages'] > 0 else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {bucket.replace('_', ' ').title():15} {count:>4} ({pct:5.1f}%) {bar}")
    
    print("═" * 60 + "\n")


def display_trend(website: str, audit_type: str, metric: str = "average_score") -> None:
    """Display trend data for a metric over time."""
    trend_data = get_trend(website, audit_type, metric)
    
    if not trend_data:
        print(f"\nNo trend data available for {website} / {audit_type}")
        return
    
    print("\n" + "═" * 60)
    print(f"  TREND: {metric.replace('_', ' ').title()}")
    print(f"  {website} / {audit_type}")
    print("═" * 60)
    
    # Find min/max for scaling
    values = [v for _, v in trend_data]
    min_val = min(values)
    max_val = max(values)
    range_val = max_val - min_val if max_val != min_val else 1
    
    print(f"\n{'Date':<12} {'Value':>8}  {'Trend':^30}")
    print("-" * 60)
    
    prev_value = None
    for timestamp, value in trend_data:
        date = timestamp[:10]
        
        # Calculate bar position
        normalized = (value - min_val) / range_val
        bar_pos = int(normalized * 25)
        bar = " " * bar_pos + "●" + " " * (25 - bar_pos)
        
        # Change indicator
        if prev_value is not None:
            if value > prev_value:
                change = "▲"
            elif value < prev_value:
                change = "▼"
            else:
                change = "─"
        else:
            change = " "
        
        print(f"{date:<12} {value:>8.1f}  [{bar}] {change}")
        prev_value = value
    
    print("-" * 60)
    print(f"Min: {min_val:.1f}  Max: {max_val:.1f}  Current: {values[-1]:.1f}")
    
    # Calculate overall trend
    if len(values) >= 2:
        overall_change = values[-1] - values[0]
        if overall_change > 0:
            print(f"Overall Trend: +{overall_change:.1f} ▲")
        elif overall_change < 0:
            print(f"Overall Trend: {overall_change:.1f} ▼")
        else:
            print("Overall Trend: No change")
    
    print("═" * 60 + "\n")


def display_comparison(website: str, run_id_old: str, run_id_new: str) -> None:
    """Display comparison between two runs."""
    comparison = compare_runs(website, run_id_old, run_id_new)
    
    if not comparison:
        print(f"\nCould not compare runs: {run_id_old} vs {run_id_new}")
        print("Make sure both run IDs exist for the specified website.")
        return
    
    print_comparison_summary(comparison)


def export_comparison(website: str, run_id_old: str, run_id_new: str, output_file: str) -> None:
    """Export comparison data to JSON file."""
    comparison = compare_runs(website, run_id_old, run_id_new)
    
    if not comparison:
        print(f"\nCould not generate comparison for export")
        return
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(comparison, f, indent=2)
    
    print(f"\nComparison exported to: {output_file}")


def list_audit_types() -> None:
    """List all supported audit types."""
    print("\n" + "═" * 50)
    print("  SUPPORTED AUDIT TYPES")
    print("═" * 50)
    
    for audit_type, config in AUDIT_SCORE_CONFIG.items():
        score_range = config['score_range']
        print(f"\n  {audit_type}")
        print(f"    Score Range: {score_range[0]}-{score_range[1]}")
        print(f"    Buckets:")
        for min_v, max_v, label in config['buckets']:
            print(f"      {label}: {min_v}-{max_v}")
    
    print("\n" + "═" * 50 + "\n")


# ============================================================================
# MAIN CLI
# ============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Compare Website LLM Analyzer audit runs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Compare last two runs for a website/audit
  python compare_audits.py --website example.com --audit SEO_AUDIT

  # Compare specific runs
  python compare_audits.py --website example.com \\
      --run-old 2026-01-15_seo_audit --run-new 2026-02-01_seo_audit

  # Show full history for a website
  python compare_audits.py --website example.com --history

  # Show history filtered by audit type
  python compare_audits.py --website example.com --audit SEO_AUDIT --history

  # Show trend over time
  python compare_audits.py --website example.com --audit SEO_AUDIT --trend

  # Show specific run details
  python compare_audits.py --website example.com --run 2026-02-01_seo_audit --details

  # Export comparison to JSON
  python compare_audits.py --website example.com --audit SEO_AUDIT \\
      --export comparison.json

  # List supported audit types
  python compare_audits.py --list-audits
        '''
    )
    
    # Website selection
    parser.add_argument(
        '--website', '-w',
        type=str,
        help='Website domain (e.g., example.com)'
    )
    
    # Audit type selection
    parser.add_argument(
        '--audit', '-a',
        type=str,
        help='Audit type (e.g., SEO_AUDIT, GEO_AUDIT)'
    )
    
    # Run selection for comparison
    parser.add_argument(
        '--run-old',
        type=str,
        help='Older run ID for comparison'
    )
    
    parser.add_argument(
        '--run-new',
        type=str,
        help='Newer run ID for comparison'
    )
    
    parser.add_argument(
        '--run',
        type=str,
        help='Single run ID (for --details)'
    )
    
    # Display modes
    parser.add_argument(
        '--history', '-H',
        action='store_true',
        help='Show all historical runs'
    )
    
    parser.add_argument(
        '--trend', '-t',
        action='store_true',
        help='Show trend over time'
    )
    
    parser.add_argument(
        '--details', '-d',
        action='store_true',
        help='Show detailed info for a specific run (requires --run)'
    )
    
    parser.add_argument(
        '--metric', '-m',
        type=str,
        default='average_score',
        help='Metric to track for trend (default: average_score)'
    )
    
    # Output options
    parser.add_argument(
        '--export', '-e',
        type=str,
        metavar='FILE',
        help='Export comparison to JSON file'
    )
    
    parser.add_argument(
        '--json',
        action='store_true',
        help='Output in JSON format'
    )
    
    # Utility options
    parser.add_argument(
        '--list-audits',
        action='store_true',
        help='List all supported audit types'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='WARNING',
        help='Set logging level (default: WARNING)'
    )
    
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    setup_logging(level=args.log_level)
    
    # Utility commands that don't require website
    if args.list_audits:
        list_audit_types()
        return 0
    
    # Commands that require website
    if not args.website and not (args.run_old and args.run_new):
        print("Error: --website is required (or both --run-old and --run-new)")
        print("Use --help for usage information")
        return 1
    
    website = args.website
    audit_type = args.audit.upper() if args.audit else None
    
    # History display
    if args.history:
        display_history(website, audit_type)
        return 0
    
    # Trend display
    if args.trend:
        if not audit_type:
            print("Error: --audit is required for trend display")
            return 1
        display_trend(website, audit_type, args.metric)
        return 0
    
    # Single run details
    if args.details:
        if not args.run:
            print("Error: --run is required for details display")
            return 1
        display_run_details(website, args.run)
        return 0
    
    # Comparison mode
    run_old = args.run_old
    run_new = args.run_new
    
    # Auto-detect last two runs if not specified
    if not run_old or not run_new:
        if not audit_type:
            print("Error: --audit is required when not specifying both run IDs")
            return 1
        
        latest = get_latest_run(website, audit_type)
        previous = get_previous_run(website, audit_type)
        
        if not latest:
            print(f"No runs found for {website} / {audit_type}")
            return 1
        
        if not previous:
            print(f"Only one run found for {website} / {audit_type}")
            print("Need at least two runs to compare.")
            display_run_details(website, latest['run_id'])
            return 0
        
        run_old = previous['run_id']
        run_new = latest['run_id']
    
    # Export mode
    if args.export:
        export_comparison(website, run_old, run_new, args.export)
        return 0
    
    # JSON output mode
    if args.json:
        comparison = compare_runs(website, run_old, run_new)
        if comparison:
            print(json.dumps(comparison, indent=2))
            return 0
        else:
            print('{"error": "Could not generate comparison"}')
            return 1
    
    # Default: display comparison
    display_comparison(website, run_old, run_new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
