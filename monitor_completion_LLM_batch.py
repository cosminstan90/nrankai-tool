"""
Monitors AI batch jobs and processes results for either Mistral or OpenAI.

Author: Cosmin
Created: 2026-01-23
Updated: 2026-02-10 - Added proper logging and error handling
"""

import os
import json
import time
import re
import hashlib

# Import all configuration from centralized config module
from config import (
    client,
    PROVIDER,
    QUESTION_TYPE,
    setup_output_directory,
    get_website,
    get_question_type,
    get_provider,
    get_model_name,
)

# Import content chunker for merging multi-chunk results
from content_chunker import AuditResultMerger, ChunkMetadata

# Import prompt_loader for custom audit detection
from prompt_loader import is_custom_audit, get_audit_definition

# Import audit_builder for custom audit processing
try:
    from audit_builder import (
        get_score_prefix as get_custom_prefix,
        get_save_condition as check_custom_save_condition
    )
    AUDIT_BUILDER_AVAILABLE = True
except ImportError:
    AUDIT_BUILDER_AVAILABLE = False

# Import history tracking (optional - graceful degradation if not available)
try:
    from history_tracker import auto_archive_and_compare
    HISTORY_TRACKING_AVAILABLE = True
except ImportError:
    HISTORY_TRACKING_AVAILABLE = False

# Import logger
from logger import get_logger, setup_logging

# Initialize module logger
logger = get_logger(__name__)


def _clean_json_fences(text: str) -> str:
    """Strip markdown code fences and attempt lightweight repair of LLM JSON.

    Handles ```json ... ``` wrappers and common LLM JSON errors
    (trailing commas, missing commas between properties).
    """
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    text = text.strip()

    # Try parsing as-is first — skip repair if valid
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Light repair: trailing commas before } or ]
    repaired = re.sub(r',\s*([}\]])', r'\1', text)
    # Light repair: missing comma between properties
    repaired = re.sub(r'(")\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(})\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(])\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(\d)\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(true|false|null)\s*\n\s*(")', r'\1,\n\2', repaired)

    return repaired


def _safe_int(value, default=0) -> int:
    """Safely convert a value to int (LLMs sometimes return scores as strings)."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    return default


def _get_prefix_for_audit(audit_data: dict, question_type: str) -> str:
    """
    Determine the filename prefix based on audit type and data.

    Keys match the output_schema defined in each prompts/*.yaml file.
    """
    question_type = question_type.upper()

    # Check if this is a custom audit
    if AUDIT_BUILDER_AVAILABLE and is_custom_audit(question_type):
        definition = get_audit_definition(question_type)
        if definition:
            return get_custom_prefix(definition, audit_data)

    # Legacy audit types
    if question_type in ("GREENWASHING", "ADVERTISMENT"):
        violations = audit_data.get('violations', [])
        return f"{len(violations):02d}"

    elif question_type == "KANTAR":
        analysis = audit_data.get('analysis', {})
        total_score = (
            _safe_int(analysis.get('meaningful', {}).get('score', 0)) +
            _safe_int(analysis.get('different', {}).get('score', 0))
        )
        return f"{total_score:02d}"

    elif question_type == "RELEVANCY_AUDIT":
        relevancy = audit_data.get('relevancy_audit', {})
        prob_score = _safe_int(relevancy.get('score_current_probability', 0))
        recommendation = relevancy.get('recommendation', 'unknown')
        return f"{prob_score:02d}_{recommendation}"

    # --- Mapping: QUESTION_TYPE -> YAML output_schema root key ---
    _AUDIT_KEY_MAP = {
        "SEO_AUDIT": "seo_audit",
        "GEO_AUDIT": "geo_audit",
        "ACCESSIBILITY_AUDIT": "accessibility_audit",
        "UX_CONTENT": "ux_content_audit",
        "LEGAL_GDPR": "gdpr_audit",
        "CONTENT_QUALITY": "content_quality",
        "BRAND_VOICE": "brand_voice_audit",
        "E_COMMERCE": "ecommerce_audit",
        "TRANSLATION_QUALITY": "translation_audit",
        "INTERNAL_LINKING": "internal_linking",
        "COMPETITOR_ANALYSIS": "competitive_positioning_audit",
        "SPELLING_GRAMMAR": "spelling_grammar_audit",
        "READABILITY_AUDIT": "readability_audit",
        "TECHNICAL_SEO": "technical_seo_audit",
        "CONTENT_FRESHNESS": "freshness_audit",
        "LOCAL_SEO": "local_seo_audit",
        "SECURITY_CONTENT_AUDIT": "security_content_audit",
        "AI_OVERVIEW_OPTIMIZATION": "ai_overview_audit",
    }

    json_key = _AUDIT_KEY_MAP.get(question_type)
    if json_key:
        section = audit_data.get(json_key, {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    # Generic fallback: find any dict with overall_score
    for key, value in audit_data.items():
        if isinstance(value, dict) and 'overall_score' in value:
            score = _safe_int(value.get('overall_score', 0))
            classification = value.get('classification', '')
            if classification:
                return f"{score:03d}_{classification}"
            return f"{score:03d}"
    return "000"


def is_chunked_result(custom_id: str) -> bool:
    """Check if a result is from a chunked page."""
    return "__chunk" in custom_id


def parse_chunk_id(custom_id: str) -> tuple:
    """
    Parse a chunk custom_id to extract base filename and chunk info.
    
    Args:
        custom_id: Custom ID like "filename.txt__chunk2of3"
        
    Returns:
        Tuple of (base_filename, chunk_index, total_chunks)
        For non-chunked: (custom_id, 0, 1)
    """
    if "__chunk" not in custom_id:
        return (custom_id, 0, 1)
    
    # Parse "filename.txt__chunk2of3" format
    base_filename, chunk_part = custom_id.rsplit("__chunk", 1)
    
    # Parse "2of3" -> chunk_index=1 (0-based), total_chunks=3
    match = re.match(r'(\d+)of(\d+)', chunk_part)
    if match:
        chunk_num = int(match.group(1))  # 1-based in ID
        total = int(match.group(2))
        return (base_filename, chunk_num - 1, total)  # Convert to 0-based index
    
    return (custom_id, 0, 1)


def group_chunked_results(results_data: list) -> dict:
    """
    Group parsed results by base filename, handling both chunked and non-chunked results.
    
    Args:
        results_data: List of tuples (custom_id, audit_data)
        
    Returns:
        Dict mapping base_filename to list of (chunk_index, total_chunks, audit_data)
    """
    grouped = {}
    
    for custom_id, audit_data in results_data:
        base_filename, chunk_index, total_chunks = parse_chunk_id(custom_id)
        
        if base_filename not in grouped:
            grouped[base_filename] = []
        
        grouped[base_filename].append((chunk_index, total_chunks, audit_data))
    
    # Sort chunks by index within each group
    for base_filename in grouped:
        grouped[base_filename].sort(key=lambda x: x[0])
    
    return grouped


def merge_chunked_results(chunks: list, audit_type: str) -> dict:
    """
    Merge multiple chunk results into a single result.
    
    Args:
        chunks: List of (chunk_index, total_chunks, audit_data) tuples
        audit_type: Type of audit for merge strategy selection
        
    Returns:
        Merged audit result dictionary
    """
    if len(chunks) == 1:
        return chunks[0][2]
    
    # Extract audit data and create metadata
    results = [chunk[2] for chunk in chunks]
    
    # Create chunk metadata (estimate chunk length from audit data size)
    metadata = []
    for idx, total, data in chunks:
        # Estimate chunk length from JSON size (rough proxy)
        chunk_len = len(json.dumps(data))
        metadata.append(ChunkMetadata(
            chunk_index=idx,
            total_chunks=total,
            original_length=chunk_len,  # Not accurate but sufficient for weighting
            chunk_length=chunk_len
        ))
    
    # Use AuditResultMerger for intelligent merging
    return AuditResultMerger.merge_audit_results(results, metadata, audit_type)


def core_process_logic(jsonl_content):
    """
    Processes the provided JSONL content based on specified question types and provider-specific
    response structures. Handles multi-chunk results by detecting and merging them before saving.
    Extracted and analyzed data are saved to disk based on the context of
    violations, audit results, or relevant metrics.

    :param jsonl_content: The JSONL formatted string containing a batch of analysis results. Each
                          line represents a single JSON object with custom IDs and provider-specific
                          response data.
    :type jsonl_content: str
    :return: None
    """
    output_dir = setup_output_directory()
    results_jsonl = jsonl_content.strip().split('\n')

    logger.info(f"Processing {len(results_jsonl)} result lines")
    
    # Phase 1: Extract all results from JSONL
    extracted_results = []
    extraction_errors = 0
    
    for line in results_jsonl:
        if not line.strip():
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON line: {e}")
            extraction_errors += 1
            continue

        custom_id = data.get('custom_id', 'unknown')

        # Provider-specific response extraction
        try:
            if PROVIDER == "ANTHROPIC":
                if data.get('result', {}).get('type') == 'succeeded':
                    ai_response_str = data['result']['message']['content'][0]['text']
                else:
                    error_detail = data.get('result', {}).get('error', 'Unknown error')
                    logger.error(f"Anthropic request failed for {custom_id}: {error_detail}")
                    extraction_errors += 1
                    continue
            elif PROVIDER == "MISTRAL":
                ai_response_str = data['response']['choices'][0]['message']['content']
            else:
                ai_response_str = data['response']['body']['choices'][0]['message']['content']

            audit_data = json.loads(_clean_json_fences(ai_response_str))
            extracted_results.append((custom_id, audit_data))
            
        except (KeyError, TypeError) as e:
            logger.error(f"Error extracting AI response for {custom_id}: {e}", exc_info=True)
            extraction_errors += 1
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding AI JSON response for {custom_id}: {e}")
            extraction_errors += 1
    
    # Phase 2: Group and merge chunked results
    grouped_results = group_chunked_results(extracted_results)
    
    # Count chunks for logging
    chunked_files = sum(1 for chunks in grouped_results.values() if len(chunks) > 1)
    if chunked_files > 0:
        logger.info(f"Detected {chunked_files} files with multiple chunks - merging results")
    
    # Merge chunked results
    merged_results = {}
    for base_filename, chunks in grouped_results.items():
        if len(chunks) == 1:
            merged_results[base_filename] = chunks[0][2]  # Single chunk, use as-is
        else:
            logger.debug(f"Merging {len(chunks)} chunks for {base_filename}")
            merged_results[base_filename] = merge_chunked_results(chunks, QUESTION_TYPE)
    
    # Phase 3: Process merged results and save to disk
    saved_count = 0
    process_errors = 0
    
    for original_filename, audit_data in merged_results.items():
        try:
            # Use unified prefix logic (matches direct_analyzer.py)
            prefix = _get_prefix_for_audit(audit_data, QUESTION_TYPE)

            # For legacy types that only save conditionally
            if QUESTION_TYPE in ("GREENWASHING", "ADVERTISMENT"):
                violations = audit_data.get('violations', [])
                if len(violations) == 0:
                    continue  # Skip pages with no violations

            save_to_disk(output_dir, prefix, original_filename, audit_data)
            saved_count += 1

        except Exception as e:
            logger.error(f"Error processing result for {original_filename}: {e}", exc_info=True)
            process_errors += 1

    # Summary log
    total_errors = extraction_errors + process_errors
    logger.info(
        f"Results processed: {len(results_jsonl)} lines -> {len(merged_results)} files, "
        f"{saved_count} saved, {total_errors} errors"
    )
    if chunked_files > 0:
        logger.info(f"Merged {chunked_files} multi-chunk files")
    
    # Archive to history if tracking is available and results were saved
    if saved_count > 0:
        archive_to_history(output_dir)


def archive_to_history(output_dir: str) -> None:
    """
    Archive current results to history for tracking over time.
    
    This is called automatically after processing completes.
    Gracefully handles cases where history tracking is not available.
    
    Args:
        output_dir: The output directory containing results
    """
    if not HISTORY_TRACKING_AVAILABLE:
        logger.debug("History tracking not available - skipping archive")
        return
    
    try:
        # Get current configuration
        website = get_website()
        audit_type = get_question_type()
        provider = get_provider()
        model = get_model_name()
        
        logger.info("Archiving results to history...")
        
        # Archive and optionally print comparison
        run_meta = auto_archive_and_compare(
            website=website,
            audit_type=audit_type,
            output_dir=output_dir,
            provider=provider,
            model=model,
            print_summary=True
        )
        
        if run_meta:
            logger.info(f"Archived as run: {run_meta['run_id']}")
        else:
            logger.warning("Failed to archive run to history")
            
    except Exception as e:
        # Don't let history tracking errors break the main pipeline
        logger.warning(f"History archiving failed (non-fatal): {e}")


def save_to_disk(directory, prefix, original_filename, data):
    """
    Saves the provided data to disk as a JSON file. The file name is generated using the
    specified prefix and a sanitized version of the original filename. If the resulting
    file name exceeds the maximum allowable length, it is shortened, and a hash is appended
    to maintain uniqueness. Handles path formatting for Windows systems with long paths.

    :param directory: The directory where the file should be saved.
    :type directory: str
    :param prefix: A prefix to prepend to the sanitized filename.
    :type prefix: str
    :param original_filename: The original name of the file, used to generate the new filename.
    :type original_filename: str
    :param data: The data to be saved in JSON format.
    :type data: dict
    :return: The function does not return anything.
    :rtype: None
    """
    sanitized = re.sub(r'[\\/*?:"<>|]', '_', original_filename)
    new_filename = f"{prefix}_{sanitized}"

    if len(new_filename) > 150:
        new_filename = new_filename[:140] + "_" + hashlib.md5(new_filename.encode()).hexdigest()[:8] + ".json"
    else:
        new_filename = new_filename.replace('.txt', '.json')

    output_path = os.path.join(directory, new_filename)

    # Prepend Long Path prefix for Windows if necessary
    if os.name == 'nt' and len(os.path.abspath(output_path)) > 250:
        output_path = "\\\\?\\" + os.path.abspath(output_path)

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        logger.debug(f"Saved result to: {new_filename}")
    except Exception as e:
        logger.error(f"Failed to save {new_filename}: {e}")


def process_local_file(filename):
    """
    Processes a local file by reading its content and passing it to the core
    processing logic. If the file does not exist, an error message is printed, and
    processing is halted.

    :param filename: The path of the file to process
    :type filename: str
    :return: None
    """
    if not os.path.exists(filename):
        logger.error(f"File not found: {filename}")
        return
    
    logger.info(f"Processing local file: {filename}")
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    core_process_logic(content)


def monitor_job(job_id):
    """
    Monitors the status of a job and handles the output or termination depending on the
    job's completion state. Supports jobs from three providers: ANTHROPIC, MISTRAL and OPENAI.
    """
    logger.info(f"Starting {PROVIDER} monitor for job: {job_id}")

    # Abort early if job_id looks invalid for this provider
    if PROVIDER == "ANTHROPIC" and not str(job_id).startswith("msgbatch_"):
        logger.error(
            f"Invalid Anthropic batch job ID: '{job_id}' — must start with 'msgbatch_'. "
            f"Aborting monitor to avoid infinite retry loop."
        )
        return

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5

    while True:
        try:
            if PROVIDER == "ANTHROPIC":
                batch_job = client.messages.batches.retrieve(job_id)
                status = batch_job.processing_status
                # Anthropic uses 'ended' status and results are retrieved differently
                results_available = status == "ended"
            elif PROVIDER == "MISTRAL":
                batch_job = client.batch.jobs.get(job_id=job_id)
                status = batch_job.status
                output_file_id = batch_job.output_file
            else:  # OPENAI
                batch_job = client.batches.retrieve(job_id)
                status = batch_job.status
                output_file_id = getattr(batch_job, 'output_file_id', None)

            logger.info(f"Status at {time.strftime('%H:%M:%S')}: {status}")

            if PROVIDER == "ANTHROPIC":
                if status == "ended":
                    try:
                        # Retrieve results using Anthropic's results iterator
                        results_lines = []
                        for result in client.messages.batches.results(job_id):
                            results_lines.append(json.dumps({
                                "custom_id": result.custom_id,
                                "result": {
                                    "type": result.result.type,
                                    "message": {
                                        "content": [{"text": block.text} for block in result.result.message.content]
                                    } if result.result.type == "succeeded" else None,
                                    "error": result.result.error if result.result.type == "errored" else None
                                }
                            }))
                        content = '\n'.join(results_lines)
                        core_process_logic(content)
                        break
                    except Exception as e:
                        logger.error(f"Error retrieving Anthropic batch results: {e}", exc_info=True)
                        break
                elif status in ["canceling", "canceled"]:
                    logger.warning(f"Job was canceled")
                    break

            elif status in ['SUCCESS', 'completed']:
                # Critical check: Is the file ID actually generated?
                if output_file_id:
                    try:
                        if PROVIDER == "MISTRAL":
                            content = client.files.download(file_id=output_file_id).read().decode('utf-8')
                        else:  # OPENAI
                            content = client.files.content(output_file_id).text

                        core_process_logic(content)
                        break
                    except Exception as e:
                        logger.error(
                            f"Error retrieving content for file {output_file_id}: {e}",
                            exc_info=True
                        )
                        # On download error, wait one cycle for a retry
                else:
                    logger.warning(f"Job completed, but output_file_id is not yet available. Retrying...")

            elif status in ['FAILED', 'CANCELLED', 'TIMEOUT_EXCEEDED', 'failed', 'cancelled', 'expired']:
                logger.error(f"Job terminated with status: {status}")
                # Log error details if available (specific to OpenAI)
                if hasattr(batch_job, 'errors') and batch_job.errors:
                    logger.error(f"Job errors: {batch_job.errors}")
                break

        except Exception as e:
            logger.error(f"Error during job monitoring: {e}", exc_info=True)
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error(
                    f"Aborting monitor after {MAX_CONSECUTIVE_ERRORS} consecutive errors for job: {job_id}"
                )
                return
        else:
            consecutive_errors = 0  # Reset counter on success

        # Wait 300 seconds (5 minutes) for the next check
        time.sleep(300)


if __name__ == "__main__":
    # Setup logging
    setup_logging()
    
    # If running as a standalone monitor, provide ID here
    # monitor_job("YOUR_JOB_ID_HERE")

    # Example local process
    process_local_file("./ingwb.com/ingwb-relevancy.jsonl")
