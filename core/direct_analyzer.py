"""
Direct (non-batch) LLM Analyzer for fast processing of small-to-medium sites.

Uses async concurrent API requests for rapid analysis without batch queue delays.
Suitable for sites with ~1-50 pages where immediate results are needed.

Author: Refactored for async direct mode
Created: 2026-02-10
"""

import asyncio
import json
import hashlib
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Import async LLM clients
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mistralai import Mistral

# Progress bar
from tqdm.asyncio import tqdm as async_tqdm

# Local imports
from core import config
from core.prompt_loader import load_prompt, is_custom_audit, get_audit_definition
from core.logger import get_logger, setup_logging
from core.content_chunker import ContentChunker, ChunkMetadata, AuditResultMerger

# Import audit_builder for custom audit support
try:
    from core.audit_builder import (
        get_score_prefix as get_custom_prefix,
        get_save_condition as check_custom_save_condition
    )
    AUDIT_BUILDER_AVAILABLE = True
except ImportError:
    AUDIT_BUILDER_AVAILABLE = False

# Initialize module logger
logger = get_logger(__name__)

# ============================================================================
# COST ESTIMATION CONSTANTS (per 1M tokens)
# ============================================================================
# Pricing loaded from centralized registry (api/provider_registry.py)
try:
    from api.provider_registry import get_cost_per_million_tokens
    COST_PER_MILLION_TOKENS = get_cost_per_million_tokens()
except ImportError:
    # Fallback if running standalone (not via FastAPI)
    COST_PER_MILLION_TOKENS = {
        "GOOGLE": {
            "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30},
            "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
            "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
            "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
        },
        "ANTHROPIC": {
            "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
            "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
            "claude-opus-4-5-20251101": {"input": 15.00, "output": 75.00},
        },
        "OPENAI": {
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "gpt-4o": {"input": 2.50, "output": 10.00},
            "gpt-4-turbo": {"input": 10.00, "output": 30.00},
        },
        "MISTRAL": {
            "mistral-small-latest": {"input": 0.20, "output": 0.60},
            "mistral-medium-latest": {"input": 2.70, "output": 8.10},
            "mistral-large-latest": {"input": 2.00, "output": 6.00},
        },
    }

# Default output token estimate per page (based on typical audit responses)
ESTIMATED_OUTPUT_TOKENS_PER_PAGE = 2000

# Retry configuration
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5  # 5s, 20s, 80s — longer waits for TPM recovery

# Per-page analysis timeout: if a single page takes longer than this, skip it
PAGE_TIMEOUT_SECONDS = int(os.getenv("PAGE_ANALYSIS_TIMEOUT", "90"))


def clean_json_response(text: str) -> str:
    """Strip markdown code fences and attempt lightweight repair of LLM JSON.

    LLMs often wrap JSON in ```json ... ``` blocks and occasionally produce
    minor syntax errors (trailing commas, missing commas between properties,
    unescaped quotes inside string values, etc.).
    Repair strategy:
      1. Strip code fences
      2. Try json.loads as-is
      3. Apply light regex repairs (trailing commas, missing commas)
      4. Try json_repair library for deeper structural repair
    """
    text = text.strip()
    # Remove ```json or ``` prefix
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    # Remove trailing ```
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

    # Light repair: missing comma between "value"  "key" or }  "key" or ]  "key"
    repaired = re.sub(r'(")\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(})\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(])\s*\n\s*(")', r'\1,\n\2', repaired)
    # Also handle number/bool/null followed by "key"
    repaired = re.sub(r'(\d)\s*\n\s*(")', r'\1,\n\2', repaired)
    repaired = re.sub(r'(true|false|null)\s*\n\s*(")', r'\1,\n\2', repaired)

    # Try after light repairs
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        pass

    # Deep repair via json_repair library (handles unescaped quotes, truncated
    # responses, wrong delimiters, etc.)
    try:
        from json_repair import repair_json
        deeply_repaired = repair_json(text, return_objects=False)
        # Validate the repaired output is actually parseable
        json.loads(deeply_repaired)
        return deeply_repaired
    except Exception:
        pass

    # Return light-repaired version as last resort (caller will raise JSONDecodeError)
    return repaired


# ============================================================================
# DATA CLASSES
# ============================================================================
@dataclass
class PageResult:
    """Result from processing a single page."""
    filename: str
    success: bool
    audit_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retries: int = 0
    processing_time: float = 0.0


@dataclass
class AnalysisStats:
    """Statistics for the analysis run."""
    total_pages: int = 0
    successful: int = 0
    failed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    
    @property
    def elapsed_time(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time
    
    @property
    def pages_per_minute(self) -> float:
        if self.elapsed_time > 0:
            return (self.successful / self.elapsed_time) * 60
        return 0.0


# ============================================================================
# ASYNC LLM CLIENTS
# ============================================================================
class AsyncLLMClient:
    """Unified async interface for LLM providers."""
    
    def __init__(self, provider: str, model_name: str):
        self.provider = provider
        self.model_name = model_name
        self._client = None
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize the appropriate async client based on provider."""
        if self.provider == "ANTHROPIC":
            api_key = os.getenv("ANTHROPIC_API_KEY")
            self._client = AsyncAnthropic(api_key=api_key)
        elif self.provider == "OPENAI":
            api_key = os.getenv("OPENAI_API_KEY")
            self._client = AsyncOpenAI(api_key=api_key)
        elif self.provider == "MISTRAL":
            api_key = os.getenv("MISTRAL_API_KEY")
            self._client = Mistral(api_key=api_key)
        elif self.provider == "GOOGLE":
            api_key = os.getenv("GEMINI_API_KEY")
            from google import genai
            self._client = genai.Client(api_key=api_key)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")
    
    async def complete(
        self, 
        system_message: str, 
        user_content: str,
        max_tokens: int = 8192
    ) -> Tuple[str, int, int]:
        """
        Send a completion request to the LLM.
        
        Returns:
            Tuple of (response_text, input_tokens, output_tokens)
        """
        if self.provider == "ANTHROPIC":
            response = await self._client.messages.create(
                model=self.model_name,
                max_tokens=max_tokens,
                system=system_message,
                messages=[{"role": "user", "content": f"CONTENT: {user_content}"}]
            )
            text = response.content[0].text
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            return text, input_tokens, output_tokens
            
        elif self.provider == "OPENAI":
            response = await self._client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": f"CONTENT: {user_content}"}
                ],
                response_format={"type": "json_object"},
                max_tokens=max_tokens
            )
            text = response.choices[0].message.content
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
            return text, input_tokens, output_tokens
            
        elif self.provider == "MISTRAL":
            # Mistral SDK uses async chat complete
            response = await self._client.chat.complete_async(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": f"CONTENT: {user_content}"}
                ],
                response_format={"type": "json_object"},
                max_tokens=max_tokens
            )
            text = response.choices[0].message.content
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens
            return text, input_tokens, output_tokens
        
        elif self.provider == "GOOGLE":
            from google.genai import types
            response = await self._client.aio.models.generate_content(
                model=self.model_name,
                contents=f"CONTENT: {user_content}",
                config=types.GenerateContentConfig(
                    system_instruction=system_message,
                    max_output_tokens=max_tokens,
                    temperature=0.3,
                    response_mime_type="application/json"
                )
            )
            text = response.text
            input_tokens = response.usage_metadata.prompt_token_count or 0
            output_tokens = response.usage_metadata.candidates_token_count or 0
            return text, input_tokens, output_tokens
        
        raise ValueError(f"Unknown provider: {self.provider}")
    
    async def close(self):
        """Close the client connection."""
        if self.provider == "ANTHROPIC" and hasattr(self._client, 'close'):
            await self._client.close()
        elif self.provider == "OPENAI" and hasattr(self._client, 'close'):
            await self._client.close()
        # Mistral and Google clients don't require explicit closing


# ============================================================================
# RETRY LOGIC WITH EXPONENTIAL BACKOFF
# ============================================================================
async def retry_with_backoff(
    coro_func,
    *args,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF_SECONDS,
    **kwargs
) -> Tuple[Any, int]:
    """
    Execute a coroutine with exponential backoff retry.
    
    Returns:
        Tuple of (result, retry_count)
    
    Raises:
        The last exception if all retries fail
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            result = await coro_func(*args, **kwargs)
            return result, attempt
        except Exception as e:
            last_exception = e
            error_str = str(e).lower()
            
            # Check if this is a retryable error
            is_retryable = False
            
            # Check for rate limit or server errors in message
            if any(code_str in error_str for code_str in ['429', '500', '502', '503', '529']):
                is_retryable = True
            elif 'rate' in error_str and 'limit' in error_str:
                is_retryable = True
            elif 'overloaded' in error_str or 'capacity' in error_str:
                is_retryable = True
            elif hasattr(e, 'status_code') and e.status_code in RETRYABLE_STATUS_CODES:
                is_retryable = True
            
            if is_retryable and attempt < max_retries:
                # Exponential backoff: 1s, 4s, 16s
                wait_time = base_backoff * (4 ** attempt)
                logger.warning(
                    f"Retry {attempt + 1}/{max_retries}: {type(e).__name__} - "
                    f"waiting {wait_time:.1f}s before retry"
                )
                await asyncio.sleep(wait_time)
            else:
                # Non-retryable error or out of retries
                break
    
    raise last_exception


# ============================================================================
# PER-PROVIDER RATE LIMITER
# ============================================================================
class ProviderRateLimiter:
    """Simple token-bucket rate limiter scoped to a single process.

    Paces outbound API requests to stay within the provider's RPM ceiling.
    Designed to be shared across all concurrent tasks in one DirectAnalyzer run.
    """

    def __init__(self, requests_per_minute: int):
        # Use at least 1 rpm so we never divide by zero
        self.rpm = max(requests_per_minute, 1)
        self._min_interval = 60.0 / self.rpm
        self._last_call: float = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until we are allowed to make the next request."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()


# ============================================================================
# COST RECORDING (writes to CostRecord DB table)
# ============================================================================
async def record_cost_async(
    audit_id: Optional[str],
    website: Optional[str],
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Persist actual token usage as a CostRecord row.

    Non-fatal: any DB error is logged as a warning and swallowed so the
    main analysis pipeline is never interrupted by cost-tracking failures.
    """
    try:
        from api.models.database import CostRecord, AsyncSessionLocal
        from api.provider_registry import calculate_cost

        cost_usd = calculate_cost(provider.lower(), model, input_tokens, output_tokens)
        async with AsyncSessionLocal() as db:
            record = CostRecord(
                audit_id=audit_id,
                source="audit",
                source_id=audit_id,
                website=website,
                provider=provider.lower(),
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost_usd,
            )
            db.add(record)
            await db.commit()
    except Exception as e:
        logger.warning(f"Cost recording failed (non-fatal): {e}")


# ============================================================================
# RESULT SAVING (matches batch flow output format)
# ============================================================================
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


def get_prefix_for_audit(audit_data: Dict[str, Any], question_type: str) -> str:
    """
    Determine the filename prefix based on audit type and data.

    This matches the logic in monitor_completion_LLM_batch.py.
    For custom audits, uses the audit_builder module for prefix extraction.

    Keys must match the output_schema in each prompts/*.yaml file.
    """
    question_type = question_type.upper()

    # Check if this is a custom audit
    if AUDIT_BUILDER_AVAILABLE and is_custom_audit(question_type):
        definition = get_audit_definition(question_type)
        if definition:
            return get_custom_prefix(definition, audit_data)

    # Built-in audit types
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

    # --- v2.0 audit types (keys match prompts/*.yaml output_schema) ---

    elif question_type == "SEO_AUDIT":
        # prompts/seo_audit.yaml → "seo_audit": { "overall_score": ... }
        section = audit_data.get('seo_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "GEO_AUDIT":
        # prompts/geo_audit.yaml → "geo_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('geo_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "ACCESSIBILITY_AUDIT":
        # prompts/accessibility_audit.yaml → "accessibility_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('accessibility_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "UX_CONTENT":
        # prompts/ux_content.yaml → "ux_content_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('ux_content_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "LEGAL_GDPR":
        # prompts/legal_gdpr.yaml → "gdpr_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('gdpr_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "CONTENT_QUALITY":
        # prompts/content_quality.yaml → "content_quality": { "overall_score": ..., "classification": ... }
        section = audit_data.get('content_quality', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "BRAND_VOICE":
        # prompts/brand_voice.yaml → "brand_voice_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('brand_voice_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "E_COMMERCE":
        # prompts/e_commerce.yaml → "ecommerce_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('ecommerce_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "TRANSLATION_QUALITY":
        # prompts/translation_quality.yaml → "translation_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('translation_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "INTERNAL_LINKING":
        # prompts/internal_linking.yaml → "internal_linking": { "overall_score": ..., "classification": ... }
        section = audit_data.get('internal_linking', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "COMPETITOR_ANALYSIS":
        # prompts/competitor_analysis.yaml → "competitive_positioning_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('competitive_positioning_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "SPELLING_GRAMMAR":
        # prompts/spelling_grammar.yaml → "spelling_grammar_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('spelling_grammar_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "READABILITY_AUDIT":
        # prompts/readability_audit.yaml → "readability_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('readability_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "TECHNICAL_SEO":
        # prompts/technical_seo.yaml → "technical_seo_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('technical_seo_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "CONTENT_FRESHNESS":
        # prompts/content_freshness.yaml → "freshness_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('freshness_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "LOCAL_SEO":
        # prompts/local_seo.yaml → "local_seo_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('local_seo_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "SECURITY_CONTENT_AUDIT":
        # prompts/security_content_audit.yaml → "security_content_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('security_content_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    elif question_type == "AI_OVERVIEW_OPTIMIZATION":
        # prompts/ai_overview_optimization.yaml → "ai_overview_audit": { "overall_score": ..., "classification": ... }
        section = audit_data.get('ai_overview_audit', {})
        score = _safe_int(section.get('overall_score', 0))
        classification = section.get('classification', 'unknown')
        return f"{score:03d}_{classification}"

    else:
        # Generic fallback: try to find any top-level dict with overall_score
        for key, value in audit_data.items():
            if isinstance(value, dict) and 'overall_score' in value:
                score = _safe_int(value.get('overall_score', 0))
                classification = value.get('classification', '')
                if classification:
                    return f"{score:03d}_{classification}"
                return f"{score:03d}"
        return "000"


def save_result_to_disk(
    output_dir: str,
    original_filename: str,
    audit_data: Dict[str, Any],
    question_type: str
) -> str:
    """
    Save audit result to disk in the same format as batch flow.
    
    Returns:
        The output file path
    """
    # Get prefix based on audit type
    prefix = get_prefix_for_audit(audit_data, question_type)
    
    # Sanitize filename
    sanitized = re.sub(r'[\\/*?:"<>|]', '_', original_filename)
    new_filename = f"{prefix}_{sanitized}"
    
    # Handle long filenames
    if len(new_filename) > 150:
        new_filename = new_filename[:140] + "_" + hashlib.md5(new_filename.encode()).hexdigest()[:8] + ".json"
    else:
        new_filename = new_filename.replace('.txt', '.json')
    
    output_path = os.path.join(output_dir, new_filename)
    
    # Prepend Long Path prefix for Windows if necessary
    if os.name == 'nt' and len(os.path.abspath(output_path)) > 250:
        output_path = "\\\\?\\" + os.path.abspath(output_path)
    
    # Write the file
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(audit_data, f, indent=4)
    
    return output_path


# ============================================================================
# COST ESTIMATION
# ============================================================================
def estimate_costs(
    input_dir: str,
    max_chars: int = 30000
) -> Dict[str, Dict[str, float]]:
    """
    Estimate costs for all providers/models.
    
    Returns dict like:
    {
        "ANTHROPIC (claude-sonnet-4-20250514)": {
            "input_tokens": 50000,
            "output_tokens": 40000,
            "input_cost": 0.15,
            "output_cost": 0.60,
            "total_cost": 0.75
        },
        ...
    }
    """
    # Count total input characters
    total_chars = 0
    page_count = 0
    
    for filename in os.listdir(input_dir):
        if filename.endswith(".txt"):
            page_count += 1
            file_path = os.path.join(input_dir, filename)
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Apply truncation limit
            total_chars += min(len(content), max_chars)
    
    # Estimate tokens (rough: chars / 4)
    estimated_input_tokens = total_chars // 4
    estimated_output_tokens = page_count * ESTIMATED_OUTPUT_TOKENS_PER_PAGE
    
    results = {}
    
    for provider, models in COST_PER_MILLION_TOKENS.items():
        for model, costs in models.items():
            key = f"{provider} ({model})"
            
            input_cost = (estimated_input_tokens / 1_000_000) * costs["input"]
            output_cost = (estimated_output_tokens / 1_000_000) * costs["output"]
            
            results[key] = {
                "pages": page_count,
                "input_tokens": estimated_input_tokens,
                "output_tokens": estimated_output_tokens,
                "input_cost": input_cost,
                "output_cost": output_cost,
                "total_cost": input_cost + output_cost
            }
    
    return results


def print_cost_estimate(estimates: Dict[str, Dict[str, float]]) -> None:
    """Print a formatted cost estimate table."""
    if not estimates:
        print("No pages found for cost estimation.")
        return
    
    # Get page count from first entry
    first_entry = next(iter(estimates.values()))
    page_count = first_entry["pages"]
    
    print(f"\n{'='*70}")
    print(f"💰 Cost Estimate for {page_count} pages")
    print(f"{'='*70}")
    print(f"{'Provider (Model)':<45} {'Input':>8} {'Output':>8} {'Total':>8}")
    print(f"{'-'*70}")
    
    for provider_model, data in estimates.items():
        print(
            f"{provider_model:<45} "
            f"${data['input_cost']:>7.2f} "
            f"${data['output_cost']:>7.2f} "
            f"${data['total_cost']:>7.2f}"
        )
    
    print(f"{'='*70}")
    print(f"Estimated tokens: ~{first_entry['input_tokens']:,} input, ~{first_entry['output_tokens']:,} output")
    print()


# ============================================================================
# MAIN ASYNC ANALYZER
# ============================================================================
class DirectAnalyzer:
    """
    Async direct LLM analyzer for concurrent page processing.
    
    Provides fast analysis without batch queue delays, suitable for
    small-to-medium sites (1-50 pages).
    """
    
    def __init__(
        self,
        input_dir: str,
        output_dir: str,
        question_type: str,
        provider: str,
        model_name: str,
        max_chars: int = 30000,
        concurrency: int = 5,
        max_tokens: int = 8192,
        research_dir: Optional[str] = None,
        language: str = "English",
        audit_id: Optional[str] = None,
        website: Optional[str] = None,
        prompts_dir: Optional[str] = None,
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.question_type = question_type.upper()
        self.provider = provider.upper()
        self.model_name = model_name
        self.max_chars = max_chars
        self.concurrency = concurrency
        self.max_tokens = max_tokens
        self.research_dir = research_dir
        self.language = language
        self.audit_id = audit_id       # For cost tracking & DB logging
        self.website = website          # For cost tracking
        self.prompts_dir = prompts_dir  # Custom prompts directory (None = use default)

        # Initialize content chunker for intelligent handling of large pages
        self.chunker = ContentChunker(provider=self.provider)

        # Stats tracking
        self.stats = AnalysisStats()

        # Results storage
        self.results: List[PageResult] = []
        self.failed_pages: List[Dict[str, Any]] = []

        # Shutdown handling
        self._shutdown_requested = False
        self._pending_tasks: List[asyncio.Task] = []

        # Per-provider rate limiter (shared across all concurrent tasks)
        try:
            from api.provider_registry import get_provider_rate_limits
            limits = get_provider_rate_limits(self.provider)
            self._rate_limiter = ProviderRateLimiter(limits.requests_per_minute)
            logger.debug(f"Rate limiter: {limits.requests_per_minute} RPM for {self.provider}")
        except Exception:
            # Fallback: very conservative 60 RPM if registry unavailable
            self._rate_limiter = ProviderRateLimiter(60)
        
        # Load system message — use custom prompts_dir if specified (for prompt version switching)
        if self.prompts_dir:
            from core.prompt_loader import PromptLoader
            _loader = PromptLoader(prompts_dir=self.prompts_dir)
            self.system_message = _loader.load_prompt(question_type)
        else:
            self.system_message = load_prompt(question_type)
        if language and language.lower() != "english":
            self.system_message += (
                f"\n\nLANGUAGE INSTRUCTION: "
                f"Write ALL text values in your JSON response in {language}. "
                f"This includes: recommendations, issues, descriptions, explanations, "
                f"current_state, example_implementation, quick_wins, and any other text fields. "
                f"Keep JSON keys, field names, enum values (like 'high', 'medium', 'low', "
                f"'critical', 'major', 'minor'), and scores/numbers in English. "
                f"The content being analyzed may be in any language - analyze it as-is."
            )
        
        # Initialize client
        self.client: Optional[AsyncLLMClient] = None
    
    def _setup_signal_handlers(self):
        """Setup graceful shutdown on Ctrl+C."""
        def signal_handler(signum, frame):
            if not self._shutdown_requested:
                self._shutdown_requested = True
                logger.warning("\n⚠️  Shutdown requested - completing in-progress tasks...")
                # Cancel pending tasks
                for task in self._pending_tasks:
                    if not task.done():
                        task.cancel()
        
        signal.signal(signal.SIGINT, signal_handler)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, signal_handler)
    
    async def _process_single_page(
        self,
        filename: str,
        semaphore: asyncio.Semaphore
    ) -> PageResult:
        """
        Process a single page with rate limiting and intelligent chunking.
        
        Handles pages that exceed max_chars by splitting into chunks,
        processing each chunk, and merging the results.
        
        Args:
            filename: Name of the .txt file to process
            semaphore: Asyncio semaphore for concurrency control
        
        Returns:
            PageResult with success/failure details
        """
        start_time = time.time()
        
        async with semaphore:
            if self._shutdown_requested:
                return PageResult(
                    filename=filename,
                    success=False,
                    error="Shutdown requested"
                )

            try:
                # Read page content
                file_path = os.path.join(self.input_dir, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    page_text = f.read()

                # Inject research context if available
                if self.research_dir:
                    from perplexity_researcher import load_research_context
                    research_context = load_research_context(self.research_dir, filename)
                    if research_context:
                        page_text = page_text + "\n\n" + research_context

                # Use intelligent chunking instead of hard truncation
                chunk_result = self.chunker.chunk_content(page_text, max_chars=self.max_chars)

                # Track total tokens across all chunks
                total_input_tokens = 0
                total_output_tokens = 0
                total_retries = 0

                if chunk_result.single_chunk:
                    # Most common case: content fits in one chunk
                    # Respect provider rate limits before each API call
                    await self._rate_limiter.acquire()
                    try:
                        (response_text, input_tokens, output_tokens), retries = await asyncio.wait_for(
                            retry_with_backoff(
                                self.client.complete,
                                self.system_message,
                                chunk_result.chunks[0],
                                self.max_tokens
                            ),
                            timeout=PAGE_TIMEOUT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        logger.warning(f"⏱ Timeout ({PAGE_TIMEOUT_SECONDS}s) processing {filename} — skipping")
                        return PageResult(
                            filename=filename,
                            success=False,
                            error=f"Timeout: analysis exceeded {PAGE_TIMEOUT_SECONDS}s",
                            processing_time=time.time() - start_time
                        )

                    total_input_tokens = input_tokens
                    total_output_tokens = output_tokens
                    total_retries = retries

                    # Parse JSON response
                    try:
                        audit_data = json.loads(clean_json_response(response_text))
                    except json.JSONDecodeError as e:
                        return PageResult(
                            filename=filename,
                            success=False,
                            error=f"Invalid JSON response: {e}",
                            retries=retries,
                            processing_time=time.time() - start_time
                        )
                else:
                    # Multi-chunk case: process each chunk and merge results
                    logger.debug(f"Processing {filename} in {len(chunk_result)} chunks")

                    chunk_results = []
                    for i, chunk_text in enumerate(chunk_result.chunks):
                        metadata = chunk_result.metadata[i]

                        # Rate-limit each chunk call
                        await self._rate_limiter.acquire()
                        try:
                            (response_text, input_tokens, output_tokens), retries = await asyncio.wait_for(
                                retry_with_backoff(
                                    self.client.complete,
                                    self.system_message,
                                    chunk_text,
                                    self.max_tokens
                                ),
                                timeout=PAGE_TIMEOUT_SECONDS
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"⏱ Timeout ({PAGE_TIMEOUT_SECONDS}s) on {filename} chunk {i+1} — skipping chunk"
                            )
                            continue  # Try remaining chunks

                        total_input_tokens += input_tokens
                        total_output_tokens += output_tokens
                        total_retries += retries

                        # Parse JSON response
                        try:
                            chunk_audit = json.loads(clean_json_response(response_text))
                            chunk_results.append((metadata.chunk_index, metadata.total_chunks, chunk_audit))
                        except json.JSONDecodeError as e:
                            logger.warning(f"Invalid JSON for {filename} chunk {i+1}: {e}")
                            # Continue with other chunks

                    if not chunk_results:
                        return PageResult(
                            filename=filename,
                            success=False,
                            error="All chunks failed (timeout or invalid JSON)",
                            retries=total_retries,
                            processing_time=time.time() - start_time
                        )

                    # Merge chunk results
                    if len(chunk_results) == 1:
                        audit_data = chunk_results[0][2]
                    else:
                        # Create metadata for merging
                        results = [cr[2] for cr in chunk_results]
                        merge_metadata = []
                        for idx, total, _ in chunk_results:
                            merge_metadata.append(ChunkMetadata(
                                chunk_index=idx,
                                total_chunks=total,
                                original_length=1,  # Not used for weighting
                                chunk_length=1
                            ))

                        audit_data = AuditResultMerger.merge_audit_results(
                            results, merge_metadata, self.question_type
                        )

                # Update stats
                self.stats.total_input_tokens += total_input_tokens
                self.stats.total_output_tokens += total_output_tokens

                # Record cost to DB (non-blocking, non-fatal)
                if total_input_tokens > 0:
                    asyncio.create_task(record_cost_async(
                        audit_id=self.audit_id,
                        website=self.website,
                        provider=self.provider,
                        model=self.model_name,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    ))

                # Save to disk
                save_result_to_disk(
                    self.output_dir,
                    filename,
                    audit_data,
                    self.question_type
                )

                return PageResult(
                    filename=filename,
                    success=True,
                    audit_data=audit_data,
                    retries=total_retries,
                    processing_time=time.time() - start_time
                )

            except asyncio.CancelledError:
                return PageResult(
                    filename=filename,
                    success=False,
                    error="Task cancelled"
                )
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                logger.error(f"Failed to process {filename}: {error_msg}")
                return PageResult(
                    filename=filename,
                    success=False,
                    error=error_msg,
                    processing_time=time.time() - start_time
                )
    
    def _get_txt_files(self) -> List[str]:
        """Get list of .txt files in input directory, skipping already-processed ones.

        Output filename format: "{score}_{classification}_{sanitized_input}.json"
        e.g. "052_needs_work_ing.ro_some-page.json"
        We match by checking whether any output file ENDS WITH "_{sanitized_input}.json".
        """
        # Collect all output JSON filenames for suffix matching
        output_filenames: set = set()
        if os.path.exists(self.output_dir):
            for f in os.listdir(self.output_dir):
                if f.endswith(".json"):
                    output_filenames.add(f)

        files = []
        skipped = 0
        for filename in os.listdir(self.input_dir):
            if not filename.endswith(".txt"):
                continue
            # Reproduce the sanitized filename the way save_result_to_file does
            sanitized = re.sub(r'[\\/*?:"<>|]', '_', filename)
            expected_json = sanitized[:-4] + ".json"  # replace .txt → .json

            # A page is "done" if any output file IS or ENDS WITH _{expected_json}
            already = (
                expected_json in output_filenames
                or any(f.endswith("_" + expected_json) for f in output_filenames)
            )
            if already:
                skipped += 1
                continue
            files.append(filename)

        if skipped:
            logger.info(
                f"Resuming: skipping {skipped} already-processed pages, "
                f"{len(files)} remaining"
            )

        return sorted(files)
    
    async def run(self) -> AnalysisStats:
        """
        Run the analysis on all pages.
        
        Returns:
            AnalysisStats with run metrics
        """
        # Setup
        self._setup_signal_handlers()
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Get files to process
        txt_files = self._get_txt_files()
        self.stats.total_pages = len(txt_files)
        
        if not txt_files:
            logger.warning(f"No .txt files found in {self.input_dir}")
            return self.stats
        
        logger.info(f"🚀 Starting DIRECT mode analysis")
        logger.info(f"   Provider: {self.provider} ({self.model_name})")
        logger.info(f"   Audit type: {self.question_type}")
        logger.info(f"   Pages: {len(txt_files)}")
        logger.info(f"   Concurrency: {self.concurrency}")
        logger.info(f"   Output: {self.output_dir}")
        
        # Initialize async client
        self.client = AsyncLLMClient(self.provider, self.model_name)
        
        # Create semaphore for concurrency control
        semaphore = asyncio.Semaphore(self.concurrency)
        
        # Create tasks for all pages
        tasks = []
        for filename in txt_files:
            task = asyncio.create_task(
                self._process_single_page(filename, semaphore)
            )
            tasks.append(task)
            self._pending_tasks.append(task)
        
        # Process with progress bar
        try:
            results = []
            async for result in async_tqdm(
                asyncio.as_completed(tasks),
                total=len(tasks),
                desc="Analyzing pages",
                unit="page",
                ncols=80
            ):
                try:
                    page_result = await result
                    results.append(page_result)
                    
                    if page_result.success:
                        self.stats.successful += 1
                    else:
                        self.stats.failed += 1
                        self.failed_pages.append({
                            "filename": page_result.filename,
                            "error": page_result.error,
                            "retries": page_result.retries
                        })
                except asyncio.CancelledError:
                    pass
            
            self.results = results
            
        except Exception as e:
            logger.error(f"Error during analysis: {e}")
        finally:
            # Cleanup
            self.stats.end_time = time.time()
            if self.client:
                await self.client.close()
        
        # Save failed pages log if any
        if self.failed_pages:
            failed_path = os.path.join(self.output_dir, "failed_pages.json")
            with open(failed_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "timestamp": datetime.now().isoformat(),
                    "total_failed": len(self.failed_pages),
                    "pages": self.failed_pages
                }, f, indent=2)
            logger.warning(f"⚠️  {len(self.failed_pages)} pages failed - see {failed_path}")
        
        # Print summary
        self._print_summary()
        
        return self.stats
    
    def _print_summary(self):
        """Print analysis summary."""
        print(f"\n{'='*60}")
        print(f"📊 Analysis Complete")
        print(f"{'='*60}")
        print(f"   Total pages:     {self.stats.total_pages}")
        print(f"   Successful:      {self.stats.successful}")
        print(f"   Failed:          {self.stats.failed}")
        print(f"   Elapsed time:    {self.stats.elapsed_time:.1f}s")
        print(f"   Speed:           {self.stats.pages_per_minute:.1f} pages/min")
        print(f"   Input tokens:    {self.stats.total_input_tokens:,}")
        print(f"   Output tokens:   {self.stats.total_output_tokens:,}")
        
        if self.stats.successful > 0:
            # Calculate actual cost
            model_costs = COST_PER_MILLION_TOKENS.get(
                self.provider, {}
            ).get(self.model_name)
            
            if model_costs:
                input_cost = (self.stats.total_input_tokens / 1_000_000) * model_costs["input"]
                output_cost = (self.stats.total_output_tokens / 1_000_000) * model_costs["output"]
                total_cost = input_cost + output_cost
                print(f"   Actual cost:     ${total_cost:.4f}")
        
        print(f"   Output dir:      {self.output_dir}")
        print(f"{'='*60}\n")


# ============================================================================
# PUBLIC API
# ============================================================================
async def run_direct_analysis(
    input_dir: str,
    output_dir: str,
    question_type: str,
    provider: str,
    model_name: str,
    max_chars: int = 30000,
    concurrency: int = 5,
    research_dir: Optional[str] = None,
    language: str = "English",
    audit_id: Optional[str] = None,
    website: Optional[str] = None,
    prompts_dir: Optional[str] = None,
) -> AnalysisStats:
    """
    Run direct (non-batch) LLM analysis.

    Args:
        input_dir: Directory containing .txt files to analyze
        output_dir: Directory to save results
        question_type: Audit type (e.g., 'SEO_AUDIT', 'GEO_AUDIT')
        provider: LLM provider ('ANTHROPIC', 'OPENAI', 'MISTRAL')
        model_name: Model name to use
        max_chars: Maximum characters per request (default: 30000)
        concurrency: Number of concurrent requests (default: 5)
        research_dir: Optional directory with Perplexity research .json files
        language: Output language for recommendations (default: English)
        audit_id: Optional audit ID for cost tracking / logging
        website: Optional website label for cost tracking

    Returns:
        AnalysisStats with run metrics
    """
    analyzer = DirectAnalyzer(
        input_dir=input_dir,
        output_dir=output_dir,
        question_type=question_type,
        provider=provider,
        model_name=model_name,
        max_chars=max_chars,
        concurrency=concurrency,
        research_dir=research_dir,
        language=language,
        audit_id=audit_id,
        website=website,
        prompts_dir=prompts_dir,
    )

    return await analyzer.run()


def run_cost_estimate(input_dir: str, max_chars: int = 30000) -> None:
    """
    Print cost estimates for all providers and prompt for confirmation.
    
    Args:
        input_dir: Directory containing .txt files
        max_chars: Maximum characters per request
    
    Returns:
        True if user confirms, False otherwise
    """
    estimates = estimate_costs(input_dir, max_chars)
    print_cost_estimate(estimates)


def get_page_count(input_dir: str) -> int:
    """Count .txt files in input directory."""
    count = 0
    if os.path.isdir(input_dir):
        for filename in os.listdir(input_dir):
            if filename.endswith(".txt"):
                count += 1
    return count


async def retry_failed_pages(
    failed_pages_file: str,
    input_dir: str,
    output_dir: str,
    question_type: str,
    provider: str,
    model_name: str,
    max_chars: int = 30000,
    concurrency: int = 3
) -> AnalysisStats:
    """
    Retry only the failed pages from a previous run.
    
    Args:
        failed_pages_file: Path to failed_pages.json
        input_dir: Directory containing original .txt files
        output_dir: Directory to save results
        question_type: Audit type
        provider: LLM provider
        model_name: Model name
        max_chars: Maximum characters per request
        concurrency: Number of concurrent requests (lower for retries)
    
    Returns:
        AnalysisStats for the retry run
    """
    # Load failed pages list
    with open(failed_pages_file, 'r', encoding='utf-8') as f:
        failed_data = json.load(f)
    
    failed_filenames = [p["filename"] for p in failed_data.get("pages", [])]
    
    if not failed_filenames:
        logger.info("No failed pages to retry")
        return AnalysisStats()
    
    logger.info(f"Retrying {len(failed_filenames)} failed pages...")
    
    # Create a temporary input dir with only failed pages
    import tempfile
    import shutil
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for filename in failed_filenames:
            src = os.path.join(input_dir, filename)
            dst = os.path.join(temp_dir, filename)
            if os.path.exists(src):
                shutil.copy2(src, dst)
        
        # Run analysis on just the failed pages
        return await run_direct_analysis(
            input_dir=temp_dir,
            output_dir=output_dir,
            question_type=question_type,
            provider=provider,
            model_name=model_name,
            max_chars=max_chars,
            concurrency=concurrency
        )


# ============================================================================
# STANDALONE EXECUTION
# ============================================================================
if __name__ == "__main__":
    import argparse
    
    # Ensure config is loaded
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())
    
    parser = argparse.ArgumentParser(
        description='Direct (non-batch) LLM analyzer for fast website analysis',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Analyze with defaults (5 concurrent requests)
  python direct_analyzer.py --input-dir ./site/input_llm --output-dir ./site/output_seo --audit SEO_AUDIT
  
  # Higher concurrency for small sites
  python direct_analyzer.py --input-dir ./site/input_llm --output-dir ./site/output --audit GEO_AUDIT --concurrency 10
  
  # Cost estimate only (dry run)
  python direct_analyzer.py --input-dir ./site/input_llm --cost-estimate
  
  # Retry failed pages
  python direct_analyzer.py --retry-failed ./site/output/failed_pages.json --input-dir ./site/input_llm
        '''
    )
    
    parser.add_argument(
        '--input-dir',
        type=str,
        required=True,
        help='Directory containing .txt files to analyze'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        help='Directory to save results (default: derived from input-dir)'
    )
    
    parser.add_argument(
        '--audit', '--question',
        type=str,
        dest='audit',
        help='Audit type (e.g., SEO_AUDIT, GEO_AUDIT)'
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
        '--concurrency',
        type=int,
        default=5,
        help='Number of concurrent API requests (default: 5)'
    )
    
    parser.add_argument(
        '--max-chars',
        type=int,
        default=30000,
        help='Maximum characters per request (default: 30000)'
    )
    
    parser.add_argument(
        '--cost-estimate',
        action='store_true',
        help='Show cost estimate and exit (dry run)'
    )
    
    parser.add_argument(
        '--retry-failed',
        type=str,
        metavar='FAILED_PAGES_JSON',
        help='Path to failed_pages.json to retry only failed pages'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default='INFO',
        help='Logging level (default: INFO)'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(level=args.log_level)
    
    # Handle cost estimate mode
    if args.cost_estimate:
        run_cost_estimate(args.input_dir, args.max_chars)
        
        # Ask for confirmation
        response = input("Proceed with analysis? [Y/n] ").strip().lower()
        if response and response != 'y':
            print("Aborted.")
            sys.exit(0)
    
    # Configure
    try:
        config.configure(
            question_type=args.audit,
            provider=args.provider,
            model_name=args.model,
            max_chars=args.max_chars
        )
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    
    # Get configuration
    provider = config.get_provider()
    model_name = config.get_model_name()
    question_type = config.get_question_type()
    
    # Determine output directory
    if args.output_dir:
        output_dir = args.output_dir
    else:
        # Derive from input_dir path structure
        parent = os.path.dirname(args.input_dir.rstrip('/'))
        output_dir = os.path.join(parent, f"output_{question_type.lower()}")
    
    # Handle retry mode
    if args.retry_failed:
        if not os.path.exists(args.retry_failed):
            logger.error(f"Failed pages file not found: {args.retry_failed}")
            sys.exit(1)
        
        asyncio.run(retry_failed_pages(
            failed_pages_file=args.retry_failed,
            input_dir=args.input_dir,
            output_dir=output_dir,
            question_type=question_type,
            provider=provider,
            model_name=model_name,
            max_chars=args.max_chars,
            concurrency=args.concurrency
        ))
    else:
        # Normal run
        asyncio.run(run_direct_analysis(
            input_dir=args.input_dir,
            output_dir=output_dir,
            question_type=question_type,
            provider=provider,
            model_name=model_name,
            max_chars=args.max_chars,
            concurrency=args.concurrency
        ))
