"""
Background worker for running audit pipelines.

Executes the scraping, conversion, and analysis steps asynchronously,
updating the database with progress at each step.
"""

import os
import sys
import json
import asyncio
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure .env is loaded in background tasks
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path for imports

from api.models.database import Audit, AuditResult, AuditLog, AsyncSessionLocal
from api.utils.url_validator import validate_external_url
# track_cost imported lazily inside run_analysis_step to avoid circular import:
# audit_worker → api.routes.costs → api.routes.__init__ → audits.py → audit_worker

import logging
logger = logging.getLogger("audit_worker")


async def fire_webhook(webhook_url: str, payload: dict) -> None:
    """POST a JSON notification to webhook_url. Non-fatal — errors are only logged."""
    try:
        validate_external_url(webhook_url, "webhook_url")
    except ValueError as e:
        logger.warning(f"Webhook blocked (SSRF protection): {e}")
        return  # silently skip — don't raise, webhooks are fire-and-forget
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Content-Type": "application/json", "User-Agent": "GEO-Analyzer/2.1"},
            ) as resp:
                status = resp.status
                if status >= 400:
                    body = await resp.text()
                    print(f"[WEBHOOK] {webhook_url} responded {status}: {body[:200]}")
                else:
                    print(f"[WEBHOOK] Delivered to {webhook_url} ({status})")
    except Exception as exc:
        print(f"[WEBHOOK] Failed to deliver to {webhook_url}: {exc}")


async def get_active_audit_count(db: AsyncSession) -> int:
    """Get count of currently running audits."""
    result = await db.execute(
        select(func.count(Audit.id)).where(
            Audit.status.in_(["pending", "scraping", "converting", "analyzing"])
        )
    )
    return result.scalar()


async def log_message(audit_id: str, message: str, level: str = "INFO"):
    """Log a message for an audit."""
    async with AsyncSessionLocal() as db:
        log = AuditLog(
            audit_id=audit_id,
            level=level,
            message=message
        )
        db.add(log)
        await db.commit()


async def update_audit_status(
    audit_id: str,
    status: Optional[str] = None,
    current_step: Optional[str] = None,
    progress_percent: Optional[int] = None,
    total_pages: Optional[int] = None,
    pages_scraped: Optional[int] = None,
    pages_analyzed: Optional[int] = None,
    average_score: Optional[float] = None,
    error_message: Optional[str] = None,
    batch_job_id: Optional[str] = None
):
    """Update audit status in database."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Audit).where(Audit.id == audit_id))
        audit = result.scalar_one_or_none()
        
        if not audit:
            return
        
        if status:
            audit.status = status
            if status == "scraping":
                audit.started_at = datetime.now(timezone.utc)
            elif status in ["completed", "failed"]:
                audit.completed_at = datetime.now(timezone.utc)
        
        if current_step is not None:
            audit.current_step = current_step
        if progress_percent is not None:
            audit.progress_percent = progress_percent
        if total_pages is not None:
            audit.total_pages = total_pages
        if pages_scraped is not None:
            audit.pages_scraped = pages_scraped
        if pages_analyzed is not None:
            audit.pages_analyzed = pages_analyzed
        if average_score is not None:
            audit.average_score = average_score
        if error_message is not None:
            audit.error_message = error_message
        if batch_job_id is not None:
            audit.batch_job_id = batch_job_id
        
        await db.commit()


async def save_result(
    audit_id: str,
    page_url: str,
    filename: str,
    score: Optional[int],
    classification: Optional[str],
    result_json: Optional[dict]
):
    """Save an individual result to the database."""
    async with AsyncSessionLocal() as db:
        result = AuditResult(
            audit_id=audit_id,
            page_url=page_url,
            filename=filename,
            score=score,
            classification=classification,
            result_json=json.dumps(result_json) if result_json else None
        )
        db.add(result)
        await db.commit()


async def _run_with_retry(coro_factory, *, max_attempts: int = 3, base_delay: float = 30.0, timeout: float, label: str):
    """
    Run coro_factory() with retry on transient errors.

    coro_factory is a callable (lambda/partial) that returns a fresh coroutine each call.
    Retries on: asyncio.TimeoutError, and exceptions whose str contains
    'rate limit', '429', '503', 'overloaded', 'temporarily unavailable'.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(coro_factory(), timeout=timeout)
        except asyncio.TimeoutError:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[retry] {label} timed out (attempt {attempt}/{max_attempts}), retrying in {delay}s")
            await asyncio.sleep(delay)
        except Exception as exc:
            msg = str(exc).lower()
            is_transient = any(k in msg for k in ["rate limit", "429", "503", "overloaded", "temporarily unavailable"])
            if not is_transient or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"[retry] {label} transient error (attempt {attempt}/{max_attempts}): {exc!r}, retrying in {delay}s")
            await asyncio.sleep(delay)


def _safe_dir(url: str) -> str:
    """Convert a URL like https://example.com into a safe filesystem directory name.

    Uses sanitize_website_for_path to prevent path traversal via malicious input.
    """
    from api.utils.url_validator import sanitize_website_for_path
    return sanitize_website_for_path(url)


async def run_scraping_step(
    audit_id: str,
    website: str,
    sitemap_url: Optional[str]
) -> bool:
    """
    Run the web scraping step.
    
    Returns True if successful, False otherwise.
    """
    await update_audit_status(
        audit_id,
        status="scraping",
        current_step="Step 1/4: Scraping website...",
        progress_percent=5
    )
    await log_message(audit_id, f"Starting web scrape for {website}")
    
    if not sitemap_url:
        await log_message(audit_id, "No sitemap URL provided, skipping scrape", "WARNING")
        return True
    
    try:
        # Import scraper
        from core import web_scraper
        
        # Determine output directory
        output_dir = os.path.join(_safe_dir(website), "input_html")
        os.makedirs(output_dir, exist_ok=True)
        
        abs_output = os.path.abspath(output_dir)
        await log_message(audit_id, f"Scraping to: {abs_output}")
        
        # Skip scraping entirely if HTML files already exist
        existing_html = [f for f in os.listdir(output_dir) if f.endswith('.html')] if os.path.exists(output_dir) else []
        if len(existing_html) > 0:
            await log_message(audit_id, f"Found {len(existing_html)} existing HTML files - skipping scrape step.")
            await update_audit_status(audit_id, total_pages=len(existing_html), pages_scraped=len(existing_html), progress_percent=25)
            return True

        await log_message(audit_id, f"Fetching sitemap: {sitemap_url}")
        await update_audit_status(audit_id, progress_percent=10)
        
        # Run scraper (this is synchronous, so we run in executor)
        loop = asyncio.get_event_loop()
        
        def scrape_progress_cb(current, total):
            if total > 0:
                pct = 10 + int((current / total) * 15)
            else:
                pct = 10
            try:
                coro = update_audit_status(
                    audit_id,
                    total_pages=total,
                    pages_scraped=current,
                    progress_percent=pct
                )
                asyncio.run_coroutine_threadsafe(coro, loop)
            except Exception as e:
                print(f"Error in scrape_progress_cb for audit {audit_id}: {e}")

        await loop.run_in_executor(
            None,
            lambda: web_scraper.scrape(
                website=website,
                sitemap=sitemap_url,
                output_dir=output_dir,
                no_proxy=True,
                delay_range=(1.0, 2.0),
                progress_callback=scrape_progress_cb
            )
        )
        
        # Count scraped files
        scraped_files = len([f for f in os.listdir(output_dir) if f.endswith('.html')])
        await update_audit_status(
            audit_id,
            total_pages=scraped_files,
            pages_scraped=scraped_files,
            progress_percent=25
        )
        await log_message(audit_id, f"Scraping complete: {scraped_files} pages downloaded")
        
        if scraped_files == 0:
            abs_out = os.path.abspath(output_dir)
            all_files = os.listdir(output_dir) if os.path.exists(output_dir) else []
            await log_message(
                audit_id,
                f"Scraping produced 0 HTML files. Directory: {abs_out}, contents: {all_files[:10]}. "
                f"Check if sitemap URL is valid and contains page URLs.",
                "ERROR"
            )
            return False
        
        return True
        
    except Exception as e:
        await log_message(audit_id, f"Scraping failed: {str(e)}", "ERROR")
        traceback.print_exc()
        return False


async def run_conversion_step(audit_id: str, website: str) -> bool:
    """
    Run the HTML to text conversion step.
    
    Returns True if successful, False otherwise.
    """
    await update_audit_status(
        audit_id,
        status="converting",
        current_step="Step 2/4: Converting HTML to text...",
        progress_percent=30
    )
    await log_message(audit_id, "Starting HTML to text conversion")
    
    try:
        from core import html2llm_converter
        
        input_dir = os.path.join(_safe_dir(website), "input_html")
        output_dir = os.path.join(_safe_dir(website), "input_llm")

        os.makedirs(output_dir, exist_ok=True)
        
        if not os.path.exists(input_dir):
            await log_message(audit_id, f"Input directory not found: {input_dir}", "ERROR")
            # Check if files are in a different location
            abs_path = os.path.abspath(input_dir)
            await log_message(audit_id, f"Absolute path checked: {abs_path}", "ERROR")
            return False
        
        # Check we have HTML files to convert
        html_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.html')]
        if len(html_files) == 0:
            await log_message(audit_id, f"No HTML files found in {input_dir}", "ERROR")
            abs_path = os.path.abspath(input_dir)
            all_files = os.listdir(input_dir)
            await log_message(audit_id, f"Absolute path: {abs_path}, contents: {all_files[:10]}", "ERROR")
            return False
        
        await log_message(audit_id, f"Found {len(html_files)} HTML files to convert")
        
        # Check if text files already exist (reuse from previous audit)
        existing_txt = [f for f in os.listdir(output_dir) if f.endswith('.txt')] if os.path.exists(output_dir) else []
        if len(existing_txt) > 0:
            await log_message(audit_id, f"Found {len(existing_txt)} existing text files - reusing (skip re-conversion)")
            await update_audit_status(audit_id, progress_percent=40)
            return True
        
        # Run conversion (synchronous)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: html2llm_converter.process_directories_recursively(input_dir, output_dir)
        )
        
        # Count converted files
        converted_files = len([f for f in os.listdir(output_dir) if f.endswith('.txt')])
        await update_audit_status(audit_id, progress_percent=40)
        await log_message(audit_id, f"Conversion complete: {converted_files} text files created")
        
        if converted_files == 0:
            await log_message(audit_id, "WARNING: Conversion produced 0 text files. HTML files may be empty or unreadable.", "WARNING")
            abs_in = os.path.abspath(input_dir)
            abs_out = os.path.abspath(output_dir)
            await log_message(audit_id, f"Input: {abs_in}, Output: {abs_out}", "WARNING")
        
        return True
        
    except Exception as e:
        await log_message(audit_id, f"Conversion failed: {str(e)}", "ERROR")
        traceback.print_exc()
        return False


async def run_analysis_step(
    audit_id: str,
    website: str,
    audit_type: str,
    provider: str,
    model: str,
    max_chars: int,
    use_direct_mode: bool,
    concurrency: int,
    research_dir: Optional[str] = None,
    language: str = "English",
    prompt_version: str = "v3",
) -> bool:
    """
    Run the LLM analysis step.
    
    Returns True if successful, False otherwise.
    """
    await update_audit_status(
        audit_id,
        status="analyzing",
        current_step="Step 3/4: Analyzing with LLM...",
        progress_percent=45
    )
    await log_message(audit_id, f"Starting LLM analysis ({provider}/{model})")
    
    try:
        # Set up environment for config module
        os.environ['WEBSITE'] = website
        os.environ['QUESTION'] = audit_type
        
        input_dir = os.path.join(_safe_dir(website), "input_llm")
        # Use version-suffixed output dir for non-default prompt versions
        # so v2 results don't conflict with existing v3 results on disk
        output_suffix = f"_{prompt_version}" if prompt_version and prompt_version != "v3" else ""
        output_dir = os.path.join(_safe_dir(website), f"output_{audit_type.lower()}{output_suffix}")

        os.makedirs(output_dir, exist_ok=True)
        
        if not os.path.exists(input_dir):
            await log_message(audit_id, f"Input directory not found: {input_dir}", "ERROR")
            return False
        
        # Count input files
        input_files = [f for f in os.listdir(input_dir) if f.endswith('.txt')]
        total_pages = len(input_files)
        
        await update_audit_status(audit_id, total_pages=total_pages)
        await log_message(audit_id, f"Found {total_pages} pages to analyze")
        
        if total_pages == 0:
            await log_message(audit_id, f"No .txt files found in {input_dir}. Check if conversion step produced files.", "ERROR")
            # List what IS in the directory for debugging
            all_files = os.listdir(input_dir) if os.path.exists(input_dir) else []
            await log_message(audit_id, f"Directory contents ({len(all_files)} files): {all_files[:10]}", "ERROR")
            return False
        
        if use_direct_mode:
            # Resolve prompts directory from version
            _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if prompt_version == "v2":
                prompts_dir = os.path.join(_base_dir, "prompts_backup")
            else:
                prompts_dir = os.path.join(_base_dir, "prompts")
            await log_message(audit_id, f"Using prompt version: {prompt_version} ({prompts_dir})")

            # Use direct analyzer for faster processing
            await log_message(audit_id, f"Using direct mode with {concurrency} concurrent requests")
            if research_dir and os.path.exists(research_dir):
                await log_message(audit_id, f"Research context available from: {research_dir}")
            if language != "English":
                await log_message(audit_id, f"Output language: {language}")

            from core.direct_analyzer import run_direct_analysis


            stats = await run_direct_analysis(
                input_dir=input_dir,
                output_dir=output_dir,
                question_type=audit_type,
                provider=provider.upper(),
                model_name=model,
                max_chars=max_chars,
                concurrency=concurrency,
                research_dir=research_dir,
                language=language,
                audit_id=audit_id,
                website=website,
                prompts_dir=prompts_dir,
            )
            
            # Update pages_analyzed from actual output files
            output_files = [f for f in os.listdir(output_dir) if f.endswith('.json')]
            pages_analyzed = len(output_files)
            await update_audit_status(audit_id, pages_analyzed=pages_analyzed)
            await log_message(audit_id, f"Direct analysis complete: {pages_analyzed}/{total_pages} pages processed")

            if stats and stats.total_input_tokens:
                from api.routes.costs import track_cost
                await track_cost(
                    source="audit",
                    provider=provider,
                    model=model,
                    input_tokens=stats.total_input_tokens,
                    output_tokens=stats.total_output_tokens,
                    audit_id=audit_id,
                    website=website,
                )
        else:
            # Use batch mode
            await log_message(audit_id, "Using batch mode (slower but cost-effective)")
            
            from core import website_llm_analyzer
            from core.monitor_completion_LLM_batch import monitor_job
            
            safe_website = _safe_dir(website)
            batch_file = os.path.join(safe_website, f"{safe_website}_{provider}.jsonl")
            
            # Configure and prepare batch
            from core import config
            config.configure(
                website=website,
                question_type=audit_type,
                provider=provider,
                model_name=model,
                max_chars=max_chars
            )
            
            website_llm_analyzer.prepare_batch_file(input_dir, batch_file, max_chars)
            
            # Submit batch job
            job_id = website_llm_analyzer.start_batch_job(batch_file)
            await update_audit_status(audit_id, batch_job_id=job_id)
            await log_message(audit_id, f"Batch job submitted: {job_id}")

            # Validate job_id before monitoring (Anthropic requires msgbatch_ prefix)
            if not job_id:
                raise ValueError("Batch job submission returned empty job ID")
            if provider.upper() == "ANTHROPIC" and not str(job_id).startswith("msgbatch_"):
                raise ValueError(
                    f"Invalid Anthropic batch job ID: '{job_id}' — expected 'msgbatch_...' prefix. "
                    f"The batch submission may have failed silently."
                )

            # Monitor job (synchronous)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: monitor_job(job_id))

            # Update pages_analyzed from output files (same as direct mode)
            output_files = [f for f in os.listdir(output_dir) if f.endswith('.json')]
            pages_analyzed_count = len(output_files)
            await update_audit_status(audit_id, pages_analyzed=pages_analyzed_count)
            await log_message(audit_id, f"Batch analysis complete: {pages_analyzed_count}/{total_pages} pages processed")

        await update_audit_status(audit_id, progress_percent=90)
        await log_message(audit_id, "LLM analysis complete")
        
        return True
        
    except Exception as e:
        traceback.print_exc()
        try:
            await log_message(audit_id, f"Analysis failed: {str(e)}", "ERROR")
        except Exception:
            pass
        return False


async def run_scoring_step(audit_id: str, website: str, audit_type: str, prompt_version: str = "v3") -> bool:
    """
    Run the scoring and result collection step.

    Returns True if successful, False otherwise.
    """
    await update_audit_status(
        audit_id,
        current_step="Step 4/4: Processing results...",
        progress_percent=92
    )
    await log_message(audit_id, "Processing and scoring results")

    # Clear existing results before re-inserting (prevents duplicates on retry)
    from sqlalchemy import delete as _sa_delete
    async with AsyncSessionLocal() as _db:
        deleted = await _db.execute(
            _sa_delete(AuditResult).where(AuditResult.audit_id == audit_id)
        )
        await _db.commit()
        if deleted.rowcount:
            await log_message(audit_id, f"Cleared {deleted.rowcount} previous results before re-scoring")

    try:
        import re
        
        output_suffix = f"_{prompt_version}" if prompt_version and prompt_version != "v3" else ""
        output_dir = os.path.join(_safe_dir(website), f"output_{audit_type.lower()}{output_suffix}")

        if not os.path.exists(output_dir):
            await log_message(audit_id, f"Output directory not found: {output_dir}", "WARNING")
            return True
        
        # Count and log available files
        json_files = [f for f in os.listdir(output_dir) if f.endswith('.json')]
        await log_message(audit_id, f"Found {len(json_files)} JSON result files in {os.path.abspath(output_dir)}")
        if len(json_files) > 0:
            await log_message(audit_id, f"Sample files: {json_files[:5]}")
        
        # Process JSON result files
        prefix_pattern = re.compile(r'^(\d{2,3})')
        results = []
        total_score = 0
        score_count = 0
        
        for filename in os.listdir(output_dir):
            if not filename.endswith('.json'):
                continue
            
            filepath = os.path.join(output_dir, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    result_data = json.load(f)
                
                # Extract score from filename
                match = prefix_pattern.match(filename)
                score = int(match.group(1)) if match else None
                
                # Try to get score from JSON if not in filename
                if score is None:
                    # Try all known YAML output_schema root keys
                    for key in ['seo_audit', 'geo_audit', 'accessibility_audit',
                                'ux_content_audit', 'gdpr_audit', 'content_quality',
                                'brand_voice_audit', 'ecommerce_audit', 'translation_audit',
                                'internal_linking', 'competitive_positioning_audit',
                                'spelling_grammar_audit', 'readability_audit', 'technical_seo_audit',
                                'freshness_audit', 'local_seo_audit', 'security_content_audit',
                                'ai_overview_audit', 'score', 'overall_score']:
                        if key in result_data:
                            val = result_data[key]
                            if isinstance(val, dict):
                                for score_key in ['overall_score', 'score']:
                                    if score_key in val:
                                        try:
                                            score = int(val[score_key])
                                        except (ValueError, TypeError):
                                            continue
                                        break
                            elif isinstance(val, (int, float)):
                                score = int(val)
                            elif isinstance(val, str):
                                try:
                                    score = int(val)
                                except ValueError:
                                    pass
                            if score is not None:
                                break
                
                # Determine classification based on score
                classification = None
                if score is not None:
                    if score >= 85:
                        classification = "excellent"
                    elif score >= 70:
                        classification = "good"
                    elif score >= 50:
                        classification = "needs_work"
                    else:
                        classification = "poor"
                    
                    total_score += score
                    score_count += 1
                
                # Reconstruct URL from filename
                page_url = filename.replace('.json', '').lstrip('0123456789_')
                
                # Save to database
                await save_result(
                    audit_id=audit_id,
                    page_url=page_url,
                    filename=filename,
                    score=score,
                    classification=classification,
                    result_json=result_data
                )
                
                results.append({
                    'filename': filename,
                    'score': score,
                    'classification': classification
                })
                
            except Exception as e:
                await log_message(audit_id, f"Error processing {filename}: {str(e)}", "WARNING")
        
        # Calculate average score
        average_score = round(total_score / score_count, 1) if score_count > 0 else None
        
        await update_audit_status(
            audit_id,
            pages_analyzed=len(results),
            average_score=average_score,
            progress_percent=98
        )
        
        await log_message(
            audit_id,
            f"Processed {len(results)} results. Average score: {average_score or 'N/A'}"
        )
        
        return True
        
    except Exception as e:
        await log_message(audit_id, f"Scoring failed: {str(e)}", "ERROR")
        traceback.print_exc()
        return False


async def run_research_step(
    audit_id: str,
    website: str,
    audit_type: str
) -> Optional[str]:
    """
    Run Perplexity research step (Step 2.5).
    
    Returns research_dir path if successful, None if skipped/failed.
    """
    await update_audit_status(
        audit_id,
        current_step="Step 2.5/4: AI Search Research (Perplexity)...",
        progress_percent=42
    )
    await log_message(audit_id, "Starting Perplexity AI search research")
    
    try:
        from core.perplexity_researcher import PerplexityResearcher
        
        input_dir = os.path.join(website, "input_llm")
        research_dir = os.path.join(website, "research")
        
        researcher = PerplexityResearcher()
        
        async def progress_cb(filename, done, total):
            progress = 42 + int((done / total) * 3)  # 42-45% range
            await update_audit_status(audit_id, progress_percent=min(progress, 45))
        
        results = await researcher.research_all_pages(
            input_dir=input_dir,
            output_dir=research_dir,
            website=website,
            audit_type=audit_type,
            progress_callback=progress_cb
        )
        
        await researcher.close()
        
        brand_mentions = sum(
            1 for r in results.values()
            if any(rr.mentions_brand or rr.mentions_site for rr in r.results)
        )
        
        await log_message(
            audit_id,
            f"Research complete: {len(results)} pages researched, "
            f"{brand_mentions} with brand/site mentions in AI results"
        )
        
        return research_dir
        
    except ValueError as e:
        # Missing API key
        await log_message(audit_id, f"Perplexity research skipped: {str(e)}", "WARNING")
        return None
    except Exception as e:
        await log_message(audit_id, f"Research step failed: {str(e)}", "WARNING")
        traceback.print_exc()
        return None


async def start_audit_pipeline(
    audit_id: str,
    website: str,
    sitemap_url: Optional[str],
    audit_type: str,
    provider: str,
    model: str,
    max_chars: int = 30000,
    use_direct_mode: bool = True,
    concurrency: int = 5,
    use_perplexity: bool = False,
    language: str = "English",
    webhook_url: Optional[str] = None,
    prompt_version: str = "v3",
):
    """
    Main entry point for running the audit pipeline.
    
    This is called as a background task from the API.
    """
    try:
        await log_message(audit_id, f"Starting audit pipeline for {website}")
        await log_message(audit_id, f"Audit type: {audit_type}, Provider: {provider}")
        
        # Step 1: Scraping
        if sitemap_url:
            try:
                success = await asyncio.wait_for(
                    run_scraping_step(audit_id, website, sitemap_url),
                    timeout=1800,
                )
            except asyncio.TimeoutError:
                await update_audit_status(audit_id, status="failed", error_message="Scraping timed out after 30 minutes")
                return
            if not success:
                await update_audit_status(
                    audit_id,
                    status="failed",
                    error_message="Scraping step failed"
                )
                return
        else:
            await log_message(audit_id, "Skipping scrape step (no sitemap provided)")
            await update_audit_status(audit_id, progress_percent=25)
        
        # Step 2: Conversion (only needed when sitemap scraping was done)
        if sitemap_url:
            try:
                success = await asyncio.wait_for(
                    run_conversion_step(audit_id, website),
                    timeout=600,
                )
            except asyncio.TimeoutError:
                await update_audit_status(audit_id, status="failed", error_message="Conversion timed out after 10 minutes")
                return
            if not success:
                await update_audit_status(
                    audit_id,
                    status="failed",
                    error_message="Conversion step failed"
                )
                return
        
        # Step 2.5: Perplexity Research (optional)
        research_dir = None
        if use_perplexity:
            research_dir = await run_research_step(audit_id, website, audit_type)
            # Research failure is non-fatal - continue without it
        
        # Step 3: Analysis (with retry on transient errors, 2-hour timeout per attempt)
        try:
            success = await _run_with_retry(
                lambda: run_analysis_step(
                    audit_id, website, audit_type, provider, model,
                    max_chars, use_direct_mode, concurrency, research_dir, language,
                    prompt_version=prompt_version,
                ),
                max_attempts=3,
                base_delay=30.0,
                timeout=7200,
                label="analysis_step",
            )
        except asyncio.TimeoutError:
            await update_audit_status(audit_id, status="failed", error_message="Analysis timed out after 2 hours (all attempts exhausted)")
            return
        except Exception as exc:
            await update_audit_status(audit_id, status="failed", error_message=f"Analysis step error: {exc}")
            return
        if not success:
            await update_audit_status(
                audit_id,
                status="failed",
                error_message="Analysis step failed"
            )
            return
        
        # Step 4: Scoring
        try:
            success = await asyncio.wait_for(
                run_scoring_step(audit_id, website, audit_type, prompt_version=prompt_version),
                timeout=300,
            )
        except asyncio.TimeoutError:
            await update_audit_status(audit_id, status="failed", error_message="Scoring timed out after 5 minutes")
            return
        if not success:
            await update_audit_status(
                audit_id,
                status="failed",
                error_message="Scoring step failed"
            )
            return
        
        # Mark as completed
        await update_audit_status(
            audit_id,
            status="completed",
            current_step="Complete",
            progress_percent=100
        )
        await log_message(audit_id, "✓ Audit pipeline completed successfully")

        # Fire completion webhook (non-fatal)
        if webhook_url:
            await fire_webhook(webhook_url, {
                "event": "audit.completed",
                "audit_id": audit_id,
                "website": website,
                "audit_type": audit_type,
                "provider": provider,
                "status": "completed",
            })

    except Exception as e:
        traceback.print_exc()
        # Always try to mark the audit as failed, even if logging also fails
        try:
            await log_message(audit_id, f"Pipeline error: {str(e)}", "ERROR")
        except Exception:
            pass
        try:
            await update_audit_status(
                audit_id,
                status="failed",
                current_step="Failed",
                error_message=str(e)
            )
        except Exception as status_err:
            print(f"[CRITICAL] Could not update audit {audit_id} to failed: {status_err}")

        # Fire failure webhook (non-fatal)
        if webhook_url:
            try:
                await fire_webhook(webhook_url, {
                    "event": "audit.failed",
                    "audit_id": audit_id,
                    "website": website,
                    "audit_type": audit_type,
                    "provider": provider,
                    "status": "failed",
                    "error": str(e)[:500],
                })
            except Exception:
                pass
