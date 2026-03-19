"""
Creates and processes LLM batch requests for website analysis.

Supports both BATCH mode (for large sites) and DIRECT mode (for small-medium sites).
Mode is auto-selected based on page count, or can be forced via CLI arguments.

Author: Cosmin
Created: 2026-01-23
Updated: 2026-02-10 - Added proper logging, error handling, and DIRECT mode support
"""

import asyncio
import json
import os
import re
import sys
import time
import argparse
from monitor_completion_LLM_batch import monitor_job

# Import configuration module
import config

# Import content chunker for intelligent content handling
from content_chunker import ContentChunker

# Import logger
from logger import get_logger, setup_logging

# Initialize module logger
logger = get_logger(__name__)

# Mode selection threshold
DIRECT_MODE_THRESHOLD = 20  # Use direct mode for <= 20 pages


def get_system_message():
    """
    Load system message for the configured audit type.
    
    Returns:
        System message string from prompt YAML file
    
    Raises:
        PromptNotFoundError: If prompt file doesn't exist
    """
    from prompt_loader import load_prompt, list_available_audits, PromptNotFoundError
    
    question_type = config.get_question_type()
    
    try:
        return load_prompt(question_type)
    except PromptNotFoundError:
        available_audits = list_available_audits()
        if available_audits:
            audit_list = ", ".join([audit['type'] for audit in available_audits])
            return f"Error: QUESTION must be one of: {audit_list}"
        else:
            return "Error: No audit prompts found in prompts/ directory."


def prepare_batch_file(input_dir=None, batch_filename=None, max_chars=None):
    """
    Prepare batch file for LLM processing.
    
    Reads text files from input directory and creates JSONL batch file
    in the provider-specific format. Uses intelligent content chunking
    to handle large pages that exceed provider limits.
    
    Args:
        input_dir: Directory containing text files
        batch_filename: Output batch file path
        max_chars: Maximum characters to send per request (overrides auto-detection)
    """
    if max_chars is None:
        max_chars = config.get_max_chars()
    
    provider = config.get_provider()
    model_name = config.get_model_name()
    
    # Initialize chunker for intelligent content handling
    chunker = ContentChunker(provider=provider)
    
    requests = []
    logger.info(f"Reading text files from: {input_dir}")

    # Cache system message once — avoid repeated function calls per file
    system_message = get_system_message()

    chunked_files = 0
    total_chunks = 0
    seen_custom_ids: set = set()  # Track used custom_ids to prevent duplicates

    for filename in os.listdir(input_dir):
        if filename.endswith(".txt"):
            file_path = os.path.join(input_dir, filename)
            with open(file_path, 'r', encoding='utf-8') as f:
                page_text = f.read()

            # Use intelligent chunking instead of hard truncation
            chunk_result = chunker.chunk_content(page_text, max_chars=max_chars)

            if not chunk_result.single_chunk:
                chunked_files += 1
                total_chunks += len(chunk_result)

            for i, chunk_text in enumerate(chunk_result.chunks):
                metadata = chunk_result.metadata[i]

                # Generate custom_id with chunk suffix if multi-chunk
                # Sanitize: Anthropic/OpenAI require ^[a-zA-Z0-9_-]{1,64}$
                # Strip extension first, then replace any illegal characters
                base_name = filename.rsplit('.', 1)[0]  # remove .txt
                safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', base_name)
                # Ensure not empty after sanitization
                if not safe_name:
                    safe_name = f"page_{i}"

                if chunk_result.single_chunk:
                    raw_id = safe_name[:63]  # leave 1 char room for dedup suffix
                else:
                    chunk_suffix = f"_c{metadata.chunk_index + 1}"
                    raw_id = f"{safe_name[:63 - len(chunk_suffix)]}{chunk_suffix}"

                # Deduplicate: if truncation caused a collision, append a counter
                custom_id = raw_id
                counter = 1
                while custom_id in seen_custom_ids:
                    suffix = f"_{counter}"
                    custom_id = f"{raw_id[:64 - len(suffix)]}{suffix}"
                    counter += 1

                seen_custom_ids.add(custom_id)
                custom_id = custom_id[:64]  # final safety clamp
                
                # Provider-specific request format
                if provider == "ANTHROPIC":
                    request = {
                        "custom_id": custom_id,
                        "params": {
                            "model": model_name,
                            "max_tokens": 8192,
                            "system": system_message,
                            "messages": [
                                {"role": "user", "content": f"CONTENT: {chunk_text}"}
                            ]
                        }
                    }
                elif provider == "OPENAI":
                    body = {
                        "model": model_name,
                        "messages": [
                            {"role": "system", "content": system_message},
                            {"role": "user", "content": f"CONTENT: {chunk_text}"}
                        ],
                        "response_format": {"type": "json_object"}
                    }
                    request = {
                        "custom_id": custom_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": body
                    }
                else:  # MISTRAL
                    body = {
                        "model": model_name,
                        "messages": [
                            {"role": "system", "content": system_message},
                            {"role": "user", "content": f"CONTENT: {chunk_text}"}
                        ],
                        "response_format": {"type": "json_object"}
                    }
                    request = {
                        "custom_id": custom_id,
                        "body": body
                    }

                requests.append(request)

    with open(batch_filename, 'w', encoding='utf-8') as f:
        for req in requests:
            f.write(json.dumps(req) + '\n')

    logger.info(f"Batch file created: {batch_filename}")
    logger.info(f"Total requests: {len(requests)}")
    if chunked_files > 0:
        logger.info(f"Files requiring chunking: {chunked_files} ({total_chunks} total chunks)")
    logger.info(f"Max characters per chunk: {max_chars:,}")


def start_batch_job(batch_filename=None):
    """
    Start batch job with the configured provider.
    
    Args:
        batch_filename: Path to batch JSONL file
    
    Returns:
        Batch job ID string
    """
    provider = config.get_provider()
    model_name = config.get_model_name()
    client = config.get_client()
    
    logger.info(f"Starting batch job with provider: {provider}")
    logger.info(f"Model: {model_name}")
    
    if provider == "ANTHROPIC":
        # Read JSONL and parse into list of requests
        requests = []
        with open(batch_filename, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    requests.append(json.loads(line))

        if len(requests) == 0:
            raise ValueError(
                f"Batch file '{batch_filename}' contains 0 requests. "
                f"Check that the conversion step produced .txt files in the input_llm directory."
            )

        # Create batch using Anthropic Message Batches API
        batch_job = client.messages.batches.create(requests=requests)
        logger.info(f"Batch job created with ID: {batch_job.id}")
        return batch_job.id

    elif provider == "MISTRAL":
        with open(batch_filename, "rb") as f:
            uploaded_file = client.files.upload(
                file={"file_name": batch_filename, "content": f.read()},
                purpose="batch"
            )

        batch_job = client.batch.jobs.create(
            input_files=[uploaded_file.id],
            model=model_name,
            endpoint="/v1/chat/completions"
        )
        logger.info(f"Batch job created with ID: {batch_job.id}")
        return batch_job.id

    else:  # OPENAI
        with open(batch_filename, "rb") as f:
            uploaded_file = client.files.create(file=f, purpose="batch")
        
        batch_job = client.batches.create(
            input_file_id=uploaded_file.id,
            endpoint="/v1/chat/completions",
            completion_window="24h"
        )
        logger.info(f"Batch job created with ID: {batch_job.id}")
        return batch_job.id


def get_available_audits():
    """Get list of available audit types for argparse choices."""
    from prompt_loader import list_available_audits
    audits = list_available_audits()
    return [audit['type'] for audit in audits]


def count_input_files(input_dir: str) -> int:
    """Count .txt files in input directory."""
    if not os.path.isdir(input_dir):
        return 0
    return sum(1 for f in os.listdir(input_dir) if f.endswith('.txt'))


def parse_args():
    """Parse command line arguments."""
    # Get available audits for choices
    try:
        audit_choices = get_available_audits()
    except Exception as e:
        logger.warning(f"Could not load audit choices: {e}")
        audit_choices = []
    
    parser = argparse.ArgumentParser(
        description='Create and process LLM batch requests for website analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f'''
PROCESSING MODES:
  DIRECT mode - Uses async concurrent API calls for fast processing.
                Ideal for small-to-medium sites (1-{DIRECT_MODE_THRESHOLD} pages).
                Results available in seconds/minutes.
  
  BATCH mode  - Uses provider batch APIs for cost-effective processing.
                Ideal for large sites (>{DIRECT_MODE_THRESHOLD} pages).
                Results may take 5+ minutes due to batch queue delays.
  
  Mode is auto-selected based on page count, or use --direct/--batch to force.

Examples:
  # Use .env defaults (auto-selects mode)
  python website_llm_analyzer.py
  
  # Override audit type
  python website_llm_analyzer.py --audit SEO_AUDIT
  
  # Force direct mode for quick results
  python website_llm_analyzer.py --direct --concurrency 10
  
  # Force batch mode for large sites (cost-effective)
  python website_llm_analyzer.py --batch
  
  # Show cost estimate before running
  python website_llm_analyzer.py --cost-estimate
  
  # Dry run (show what would happen, don't submit)
  python website_llm_analyzer.py --audit GEO_AUDIT --dry-run
        '''
    )
    
    parser.add_argument(
        '--website',
        type=str,
        help='Target website domain (overrides WEBSITE in .env)'
    )
    
    parser.add_argument(
        '--audit', '--question',
        type=str,
        dest='audit',
        choices=audit_choices if audit_choices else None,
        help='Audit/question type (overrides QUESTION in .env). Available: ' + 
             ', '.join(audit_choices) if audit_choices else 'Check prompts/ directory'
    )
    
    parser.add_argument(
        '--provider',
        type=str,
        choices=['anthropic', 'openai', 'mistral'],
        help='Force specific LLM provider'
    )
    
    parser.add_argument(
        '--model',
        type=str,
        help='Override model name (e.g., claude-sonnet-4-20250514, gpt-4o)'
    )
    
    parser.add_argument(
        '--max-chars',
        type=int,
        default=30000,
        help='Maximum characters to send per request (default: 30000)'
    )
    
    # Mode selection arguments
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--direct',
        action='store_true',
        help='Force DIRECT mode (async concurrent processing, faster for small sites)'
    )
    mode_group.add_argument(
        '--batch',
        action='store_true',
        help='Force BATCH mode (queue-based processing, cost-effective for large sites)'
    )
    
    # Direct mode options
    parser.add_argument(
        '--concurrency',
        type=int,
        default=5,
        help='Number of concurrent API requests in DIRECT mode (default: 5)'
    )
    
    # Dry run / estimation options
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Create batch file but do not submit job (BATCH mode only)'
    )
    
    parser.add_argument(
        '--cost-estimate',
        action='store_true',
        help='Show cost estimate for all providers and prompt for confirmation'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Set logging level (default: INFO)'
    )
    
    return parser.parse_args()


async def run_direct_mode(input_dir: str, output_dir: str, args) -> None:
    """
    Run analysis in DIRECT mode using async concurrent processing.
    
    Args:
        input_dir: Directory containing .txt files
        output_dir: Directory to save results
        args: Parsed command line arguments
    """
    from direct_analyzer import run_direct_analysis
    
    provider = config.get_provider()
    model_name = config.get_model_name()
    question_type = config.get_question_type()
    
    await run_direct_analysis(
        input_dir=input_dir,
        output_dir=output_dir,
        question_type=question_type,
        provider=provider,
        model_name=model_name,
        max_chars=args.max_chars,
        concurrency=args.concurrency
    )


def run_batch_mode(input_dir: str, batch_file: str, args) -> None:
    """
    Run analysis in BATCH mode using provider batch APIs.
    
    Args:
        input_dir: Directory containing .txt files
        batch_file: Path for batch JSONL file
        args: Parsed command line arguments
    """
    # Prepare batch file
    prepare_batch_file(input_dir, batch_file, args.max_chars)
    
    if args.dry_run:
        logger.info("Dry run complete - batch file created but job not submitted")
        logger.info(f"Batch file location: {batch_file}")
    else:
        # Submit batch job and monitor
        job_id = start_batch_job(batch_file)
        logger.info(f"Batch submitted: job_id={job_id}")
        monitor_job(job_id)


def determine_mode(args, page_count: int) -> str:
    """
    Determine which processing mode to use.
    
    Args:
        args: Parsed command line arguments
        page_count: Number of pages to process
    
    Returns:
        'direct' or 'batch'
    """
    # Explicit mode selection
    if args.direct:
        return 'direct'
    if args.batch:
        return 'batch'
    
    # Auto-select based on page count
    if page_count <= DIRECT_MODE_THRESHOLD:
        return 'direct'
    else:
        return 'batch'


def main():
    """Main entry point with mode selection."""
    args = parse_args()
    
    # Setup logging with specified level
    setup_logging(level=args.log_level)
    
    # Configure with CLI arguments
    config.configure(
        website=args.website,
        question_type=args.audit,
        provider=args.provider,
        model_name=args.model,
        max_chars=args.max_chars,
    )
    
    # Get paths
    paths = config.get_paths()
    input_dir = paths["input_llm_dir"]
    output_dir = paths["output_dir"]
    batch_file = paths["batch_file_path"]
    
    # Ensure directories exist
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    # Count pages
    page_count = count_input_files(input_dir)
    
    if page_count == 0:
        logger.error(f"No .txt files found in {input_dir}")
        logger.error("Run the web scraper first to generate input files.")
        sys.exit(1)
    
    # Handle cost estimate
    if args.cost_estimate:
        from direct_analyzer import run_cost_estimate
        run_cost_estimate(input_dir, args.max_chars)
        
        response = input("Proceed with analysis? [Y/n] ").strip().lower()
        if response and response != 'y':
            print("Aborted.")
            sys.exit(0)
    
    # Determine mode
    mode = determine_mode(args, page_count)
    
    # Log mode selection
    print()
    if mode == 'direct':
        if args.direct:
            logger.info(f"🚀 Using DIRECT mode (forced via --direct)")
        else:
            logger.info(f"🚀 Using DIRECT mode for {page_count} pages (fast async processing)")
        logger.info(f"   Concurrency: {args.concurrency} parallel requests")
        logger.info(f"   Expected time: ~{(page_count / args.concurrency) * 2:.0f}-{(page_count / args.concurrency) * 5:.0f} seconds")
    else:
        if args.batch:
            logger.info(f"📦 Using BATCH mode (forced via --batch)")
        else:
            logger.info(f"📦 Using BATCH mode for {page_count} pages (cost-effective for large sites)")
        logger.info(f"   Note: Batch processing may take 5+ minutes due to queue delays")
    print()
    
    # Run appropriate mode
    if mode == 'direct':
        try:
            asyncio.run(run_direct_mode(input_dir, output_dir, args))
        except KeyboardInterrupt:
            logger.warning("\nInterrupted by user")
            sys.exit(130)
    else:
        run_batch_mode(input_dir, batch_file, args)


if __name__ == "__main__":
    main()
