"""
Converts HTML files to LLM-optimized text format.

Supports CLI arguments to override .env configuration.

Author: Cosmin
Created: 2026-01-23
Updated: 2026-02-10 - Added proper logging and error handling
"""

import os
import json
import html2text
import re
import warnings
import argparse
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

# Suppress BeautifulSoup warnings
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

# Import configuration module
from core import config

# Import logger
from core.logger import get_logger, setup_logging

# Initialize module logger
logger = get_logger(__name__)


def extract_content(html_content):
    """
    Extract meaningful content from an HTML document.
    
    Processes HTML by:
    1. Extracting FAQ content from JSON-LD metadata
    2. Parsing custom Shadow DOM attributes
    3. Falling back to body text extraction
    
    Args:
        html_content: Raw HTML content string
    
    Returns:
        Extracted and concatenated content string
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    extracted_segments = []

    # 1. Extract FAQs from JSON-LD metadata
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            if data.get('@type') == 'FAQPage':
                extracted_segments.append("### FREQUENTLY ASKED QUESTIONS")
                for item in data.get('mainEntity', []):
                    question = item.get('name')
                    answer_html = item.get('acceptedAnswer', {}).get('text', '')
                    answer_clean = BeautifulSoup(answer_html, 'html.parser').get_text()
                    extracted_segments.append(f"Question: {question}\nAnswer: {answer_clean}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse JSON-LD FAQ data: {e}")
            continue

    # 2. Extract text from Shadow DOM attributes
    converter = html2text.HTML2Text()
    converter.ignore_links = True
    for component in soup.find_all('ing-content-rich-text'):
        if component.has_attr('content'):
            raw_attr_html = component['content']
            clean_text = converter.handle(raw_attr_html)
            if clean_text.strip():
                extracted_segments.append(clean_text.strip())

    # 3. Fallback: Extract standard body text
    for tags in soup(['script', 'style', 'nav', 'footer', 'header']):
        tags.decompose()

    body_text = soup.get_text(separator='\n', strip=True)
    if len(body_text) > 100:
        extracted_segments.append(body_text)

    return "\n\n".join(extracted_segments)


def optimize_for_llm(text):
    """
    Refine text for LLM consumption.
    
    Performs:
    1. Removes hard line breaks within sentences
    2. Removes duplicate paragraphs
    3. Normalizes whitespace
    
    Args:
        text: Raw extracted text
    
    Returns:
        Optimized text string
    """
    # Remove hard line breaks within sentences
    text = re.sub(r'(?<![.!?])\n(?!\n)', ' ', text)

    # Deduplicate and clean lines
    lines = text.split('\n')
    unique_lines = []
    seen_content = set()

    for line in lines:
        stripped = line.strip()
        if stripped:
            lowered = stripped.lower()
            if lowered not in seen_content:
                unique_lines.append(stripped)
                seen_content.add(lowered)

    # Join with single newline
    compact_text = "\n".join(unique_lines)

    # Final cleanup
    compact_text = re.sub(r'\n{2,}', '\n', compact_text)

    return compact_text.strip()


def process_directories_recursively(input_base_dir, output_base_dir):
    """
    Recursively process all HTML files in input directory.
    
    Args:
        input_base_dir: Directory containing HTML files
        output_base_dir: Directory for output text files
    """
    if not os.path.exists(output_base_dir):
        os.makedirs(output_base_dir)

    file_count = 0
    total_chars = 0

    for root, dirs, files in os.walk(input_base_dir):
        for filename in files:
            if filename.lower().endswith(".html") and not filename.lower().endswith(".pdf.html"):
                source_path = os.path.join(root, filename)

                try:
                    with open(source_path, 'r', encoding='utf-8') as f:
                        raw_html = f.read()
                except Exception as e:
                    logger.error(f"Error reading {source_path}: {e}")
                    continue

                # Process content
                logger.debug(f"Processing: {filename}")
                raw_extracted = extract_content(raw_html)
                final_text = optimize_for_llm(raw_extracted)

                # Generate flattened filename
                relative_path = os.path.relpath(root, input_base_dir)
                if relative_path == ".":
                    new_filename = filename.rsplit('.', 1)[0] + ".txt"
                else:
                    path_prefix = relative_path.replace(os.sep, "_")
                    new_filename = f"{path_prefix}_{filename.rsplit('.', 1)[0]}.txt"

                target_path = os.path.join(output_base_dir, new_filename)

                # Write processed text
                with open(target_path, 'w', encoding='utf-8') as f_out:
                    f_out.write(final_text)

                file_count += 1
                total_chars += len(final_text)
                
                if file_count % 100 == 0:
                    logger.info(f"Processed {file_count} files...")

    # Summary log
    avg_chars = total_chars // file_count if file_count > 0 else 0
    logger.info(
        f"Conversion complete: {file_count} files processed, "
        f"avg {avg_chars:,} chars/file"
    )
    logger.info(f"Output saved to: {output_base_dir}")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Convert HTML files to LLM-optimized text format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Use .env defaults
  python html2llm_converter.py
  
  # Override website
  python html2llm_converter.py --website example.com
  
  # Custom input/output directories
  python html2llm_converter.py --input-dir ./custom_html --output-dir ./custom_text
        '''
    )
    
    parser.add_argument(
        '--website',
        type=str,
        help='Target website domain (overrides WEBSITE in .env)'
    )
    
    parser.add_argument(
        '--input-dir',
        type=str,
        help='Input directory containing HTML files (default: {website}/input_html)'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Output directory for text files (default: {website}/input_llm)'
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
    
    # Configure with CLI arguments (question_type not required for converter)
    if args.website:
        config.configure(
            website=args.website,
            question_type=os.getenv("QUESTION", "PLACEHOLDER"),  # Use placeholder since not needed
        )
    
    # Get paths
    if args.input_dir and args.output_dir:
        input_dir = args.input_dir
        output_dir = args.output_dir
    else:
        paths = config.get_paths(website_override=args.website)
        input_dir = args.input_dir or paths["input_html_dir"]
        output_dir = args.output_dir or paths["input_llm_dir"]
    
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    logger.info(f"Input directory: {input_dir}")
    logger.info(f"Output directory: {output_dir}")

    process_directories_recursively(input_dir, output_dir)
