"""
Main pipeline orchestrator for Website LLM Analyzer.

Coordinates the full analysis workflow: scrape → convert → analyze → score.

Author: Cosmin
Created: 2026-02-10
"""

import argparse
import asyncio
import sys
import os
from typing import List, Optional

# Import configuration
from core import config

# Import individual scripts
from core import web_scraper
from core import html2llm_converter
from core import website_llm_analyzer
from core import determine_score
from core import cross_reference_analyzer


def run_scraping_step(args):
    """Execute web scraping step."""
    print("\n" + "="*80)
    print("STEP 1: WEB SCRAPING")
    print("="*80)
    
    try:
        # Parse delay range
        delay_min, delay_max = map(float, args.delay.split('-'))
        delay_range = (delay_min, delay_max)
    except ValueError:
        print(f"Error: Invalid delay format '{args.delay}'. Using default 1.5-3.5")
        delay_range = (1.5, 3.5)
    
    web_scraper.scrape(
        website=args.website,
        sitemap=args.sitemap,
        output_dir=args.scrape_output,
        no_proxy=args.no_proxy,
        delay_range=delay_range,
        shadow_root_selector=getattr(args, 'shadow_root_selector', None),
    )
    
    print("✓ Scraping complete\n")


def run_conversion_step(args):
    """Execute HTML to LLM text conversion step."""
    print("\n" + "="*80)
    print("STEP 2: HTML TO LLM CONVERSION")
    print("="*80)
    
    # Get paths
    paths = config.get_paths(website_override=args.website)
    input_dir = args.convert_input or paths["input_html_dir"]
    output_dir = args.convert_output or paths["input_llm_dir"]
    
    # Ensure directories exist
    if not os.path.exists(input_dir):
        print(f"Error: Input directory does not exist: {input_dir}")
        print("Run the scraping step first or specify --convert-input")
        sys.exit(1)
    
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Input: {input_dir}")
    print(f"Output: {output_dir}")
    
    html2llm_converter.process_directories_recursively(input_dir, output_dir)
    
    print("✓ Conversion complete\n")


def run_analysis_step(args):
    """Execute LLM analysis step."""
    print("\n" + "="*80)
    print("STEP 3: LLM ANALYSIS")
    print("="*80)
    
    # Get paths
    paths = config.get_paths()
    input_dir = paths["input_llm_dir"]
    batch_file = paths["batch_file_path"]
    
    # Ensure input directory exists
    if not os.path.exists(input_dir):
        print(f"Error: Input directory does not exist: {input_dir}")
        print("Run the conversion step first")
        sys.exit(1)
    
    # Prepare batch file
    website_llm_analyzer.prepare_batch_file(
        input_dir, 
        batch_file, 
        args.max_chars
    )
    
    if args.dry_run:
        print(f"\n✓ Dry run complete. Batch file created: {batch_file}")
        print("  Skipping job submission (--dry-run mode)")
    else:
        # Submit batch job and monitor
        job_id = website_llm_analyzer.start_batch_job(batch_file)
        from monitor_completion_LLM_batch import monitor_job
        monitor_job(job_id)
        
    print("✓ Analysis complete\n")


def run_scoring_step(args):
    """Execute scoring and Excel report generation step."""
    print("\n" + "="*80)
    print("STEP 4: SCORE AGGREGATION")
    print("="*80)
    
    root_dir = args.score_root or args.website or '.'
    output_file = args.score_output or 'audit_scores.xlsx'
    
    determine_score.perform_full_audit_suite(root_dir, output_file)
    
    print("✓ Scoring complete\n")


def run_crossref_step(args):
    """Execute cross-reference analysis step."""
    print("\n" + "="*80)
    print("STEP 4.5: CROSS-REFERENCE ANALYSIS")
    print("="*80)
    
    import asyncio
    
    # Determine output directory
    paths = config.get_paths()
    output_dir = args.crossref_output or paths.get("output_dir")
    
    # Run cross-reference analysis
    asyncio.run(cross_reference_analyzer.run_cross_reference_analysis(
        website=args.website,
        audit_type=args.audit,
        output_dir=output_dir,
        provider=args.provider,
        model_name=args.model,
        no_llm=args.no_llm_crossref
    ))
    
    print("✓ Cross-reference analysis complete\n")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Website LLM Analyzer - Full Pipeline Orchestrator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
FULL PIPELINE EXAMPLES:
  # Run complete pipeline with defaults from .env
  python main.py --website example.com --audit SEO_AUDIT
  
  # Run specific steps only
  python main.py --website example.com --audit SEO_AUDIT --steps scrape,convert
  
  # Run with custom provider and dry-run mode
  python main.py --website example.com --audit GEO_AUDIT --provider anthropic --dry-run
  
  # Include cross-reference analysis
  python main.py --website example.com --audit SEO_AUDIT --steps scrape,convert,analyze,crossref,score

STEP-BY-STEP EXAMPLES:
  # 1. Scrape website
  python main.py --website example.com --steps scrape --sitemap https://example.com/sitemap.xml
  
  # 2. Convert HTML to text
  python main.py --website example.com --steps convert
  
  # 3. Analyze with LLM
  python main.py --website example.com --audit SEO_AUDIT --steps analyze
  
  # 4. Run cross-reference analysis (optional)
  python main.py --website example.com --audit SEO_AUDIT --steps crossref
  
  # 4.5 Run cross-reference without LLM (rule-based only)
  python main.py --website example.com --audit SEO_AUDIT --steps crossref --no-llm-crossref
  
  # 5. Generate report
  python main.py --website example.com --steps score

AVAILABLE STEPS:
  scrape   : Download HTML pages from website
  convert  : Convert HTML to LLM-optimized text
  analyze  : Create and process LLM batch requests
  crossref : Cross-reference analysis (site-wide patterns)
  score    : Aggregate results into Excel report
        '''
    )
    
    # Main configuration
    parser.add_argument(
        '--website',
        type=str,
        help='Target website domain (overrides WEBSITE in .env)'
    )
    
    parser.add_argument(
        '--audit', '--question',
        type=str,
        dest='audit',
        help='Audit/question type (overrides QUESTION in .env)'
    )
    
    parser.add_argument(
        '--steps',
        type=str,
        default='scrape,convert,analyze,score',
        help='Comma-separated steps to run (default: all steps). Options: scrape,convert,analyze,crossref,score'
    )
    
    # Provider configuration
    parser.add_argument(
        '--provider',
        type=str,
        choices=['anthropic', 'openai', 'mistral'],
        help='Force specific LLM provider'
    )
    
    parser.add_argument(
        '--model',
        type=str,
        help='Override model name'
    )
    
    # Scraping options
    parser.add_argument(
        '--sitemap',
        type=str,
        help='Sitemap URL (required for scrape step)'
    )
    
    parser.add_argument(
        '--no-proxy',
        action='store_true',
        help='Disable proxy for scraping'
    )
    
    parser.add_argument(
        '--delay',
        type=str,
        default='1.5-3.5',
        help='Random delay range for scraping (format: "min-max", default: "1.5-3.5")'
    )
    
    parser.add_argument(
        '--scrape-output',
        type=str,
        help='Custom output directory for scraped HTML files'
    )
    
    parser.add_argument(
        '--shadow-root-selector',
        type=str,
        default=None,
        help='CSS selector for Shadow DOM content container (e.g., "#outlet-content")'
    )
    
    # Conversion options
    parser.add_argument(
        '--convert-input',
        type=str,
        help='Custom input directory for HTML files to convert'
    )
    
    parser.add_argument(
        '--convert-output',
        type=str,
        help='Custom output directory for converted text files'
    )
    
    # Analysis options
    parser.add_argument(
        '--max-chars',
        type=int,
        default=30000,
        help='Maximum characters per LLM request (default: 30000)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Create batch file but do not submit job (analyze step only)'
    )
    
    # Cross-reference options
    parser.add_argument(
        '--crossref-output',
        type=str,
        help='Custom output directory for cross-reference analysis'
    )
    
    parser.add_argument(
        '--no-llm-crossref',
        action='store_true',
        help='Run only rule-based cross-reference analysis (faster, free)'
    )
    
    # Scoring options
    parser.add_argument(
        '--score-root',
        type=str,
        help='Root directory to scan for audit results (default: website name or ".")'
    )
    
    parser.add_argument(
        '--score-output',
        type=str,
        default='audit_scores.xlsx',
        help='Output Excel filename for scores (default: audit_scores.xlsx)'
    )
    
    return parser.parse_args()


def validate_step_requirements(step: str, args):
    """
    Validate that required arguments are present for a given step.
    
    Args:
        step: Step name ('scrape', 'convert', 'analyze', 'crossref', 'score')
        args: Parsed command line arguments
    
    Raises:
        SystemExit: If required arguments are missing
    """
    if step == 'scrape':
        if not args.sitemap and not os.getenv('SITEMAP'):
            print("Error: --sitemap is required for scraping step")
            print("Set via --sitemap argument or SITEMAP in .env")
            sys.exit(1)
    
    if step == 'analyze':
        if not args.audit and not os.getenv('QUESTION'):
            print("Error: --audit is required for analysis step")
            print("Set via --audit argument or QUESTION in .env")
            sys.exit(1)
    
    if step == 'crossref':
        if not args.audit and not os.getenv('QUESTION'):
            print("Error: --audit is required for cross-reference step")
            print("Set via --audit argument or QUESTION in .env")
            sys.exit(1)
        if not args.website and not os.getenv('WEBSITE'):
            print("Error: --website is required for cross-reference step")
            print("Set via --website argument or WEBSITE in .env")
            sys.exit(1)


def main():
    """Main pipeline orchestrator."""
    args = parse_args()
    
    # Parse steps
    steps_to_run = [s.strip().lower() for s in args.steps.split(',')]
    valid_steps = ['scrape', 'convert', 'analyze', 'crossref', 'score']
    
    # Validate steps
    invalid_steps = [s for s in steps_to_run if s not in valid_steps]
    if invalid_steps:
        print(f"Error: Invalid steps: {', '.join(invalid_steps)}")
        print(f"Valid steps are: {', '.join(valid_steps)}")
        sys.exit(1)
    
    # Validate requirements for each step
    for step in steps_to_run:
        validate_step_requirements(step, args)
    
    # Configure system
    # Note: Some steps might not need all config values
    try:
        config.configure(
            website=args.website,
            question_type=args.audit,
            sitemap=args.sitemap,
            provider=args.provider,
            model_name=args.model,
            max_chars=args.max_chars,
            no_proxy=args.no_proxy,
        )
    except ValueError as e:
        # If configuration fails, it's okay if we're only running certain steps
        if 'analyze' in steps_to_run or 'scrape' in steps_to_run or 'crossref' in steps_to_run:
            print(f"Configuration warning: {e}")
            # Re-raise if it's a critical error
            if "WEBSITE" in str(e) and args.website is None:
                sys.exit(1)
    
    # Print pipeline overview
    print("\n" + "="*80)
    print("WEBSITE LLM ANALYZER - PIPELINE EXECUTION")
    print("="*80)
    print(f"Steps to run: {' → '.join(steps_to_run)}")
    if args.website:
        print(f"Website: {args.website}")
    if args.audit:
        print(f"Audit Type: {args.audit}")
    if args.provider:
        print(f"Provider: {args.provider}")
    print("="*80)
    
    # Execute pipeline steps in order
    success = True
    
    try:
        if 'scrape' in steps_to_run:
            run_scraping_step(args)
        
        if 'convert' in steps_to_run:
            run_conversion_step(args)
        
        if 'analyze' in steps_to_run:
            run_analysis_step(args)
        
        if 'crossref' in steps_to_run:
            run_crossref_step(args)
        
        if 'score' in steps_to_run:
            run_scoring_step(args)
        
        # Final summary
        print("\n" + "="*80)
        print("✓ PIPELINE COMPLETED SUCCESSFULLY")
        print("="*80)
        print(f"Completed steps: {' → '.join(steps_to_run)}")
        print("="*80 + "\n")

        # Notify nrankai-cloud if configured
        if os.getenv("NRANKAI_CLOUD_URL"):
            from cloud_notifier import notify_audit_complete
            print("[pipeline] Notifying nrankai-cloud...")
            asyncio.run(notify_audit_complete(
                website=getattr(config, "WEBSITE", "") or os.getenv("WEBSITE", ""),
                audit_type=getattr(config, "QUESTION", "") or os.getenv("QUESTION", ""),
                prospect_id=os.getenv("PROSPECT_ID") or None,
                campaign_id=os.getenv("CAMPAIGN_ID") or None,
                scores_file=f"{args.website or os.getenv('WEBSITE', '')}/audit_scores.json",
            ))
        
    except KeyboardInterrupt:
        print("\n\nPipeline interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n✗ Pipeline failed with error:")
        print(f"  {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
