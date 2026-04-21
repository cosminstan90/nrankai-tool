"""
Answer Calibration Module (Prompt 35)
=======================================
Generates the "ideal" AI response that naturally includes the target brand,
then extracts content gaps and requirements.
Uses claude-sonnet-4-20250514 (~$0.015/calibration).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("answer_calibrator")

_MODEL = "claude-sonnet-4-20250514"
_COST_USD = 0.015


@dataclass
class CalibrationResult:
    prompt: str
    target_brand: str
    ideal_response: str = ""
    brand_position: Optional[int] = None
    content_gaps: List[str] = field(default_factory=list)
    format_requirements: List[str] = field(default_factory=list)
    data_requirements: List[str] = field(default_factory=list)
    schema_requirements: List[str] = field(default_factory=list)
    estimated_effort: str = "medium"  # low|medium|high
    cost_usd: float = _COST_USD

    def to_dict(self) -> dict:
        return {
            "prompt":               self.prompt,
            "target_brand":        self.target_brand,
            "ideal_response":      self.ideal_response,
            "brand_position":      self.brand_position,
            "content_gaps":        self.content_gaps,
            "format_requirements": self.format_requirements,
            "data_requirements":   self.data_requirements,
            "schema_requirements": self.schema_requirements,
            "estimated_effort":    self.estimated_effort,
            "cost_usd":            self.cost_usd,
        }


def _estimate_effort(content_gaps: list, data_requirements: list, schema_requirements: list) -> str:
    total = len(content_gaps) + len(data_requirements) + len(schema_requirements)
    if total <= 2:
        return "low"
    if total <= 5:
        return "medium"
    return "high"


async def calibrate(
    prompt: str,
    target_brand: str,
    target_domain: str,
    vertical: str,
    actual_fanout_result: Optional[dict] = None,
    crossref_result: Optional[dict] = None,
) -> CalibrationResult:
    """
    Generate ideal AI response including target_brand and extract requirements.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    # Build context strings
    actual_response = ""
    cited_domains: List[str] = []
    crossref_summary = ""

    if actual_fanout_result:
        actual_response = actual_fanout_result.get("full_response_text", "")[:2000]
        cited_domains = [
            s.get("source_url", "") for s in (actual_fanout_result.get("sources") or [])
        ][:5]

    if crossref_result:
        gap_count = len(crossref_result.get("gap_queries", []))
        covered   = len(crossref_result.get("covered_queries", []))
        crossref_summary = f"Cross-reference: {covered} pages covered, {gap_count} content gaps identified."

    system_prompt = (
        "You are an expert in GEO (Generative Engine Optimization). "
        "Write the ideal AI assistant response to the given query that NATURALLY includes "
        "the target brand as a recommended option. Requirements:\n"
        "- Factually plausible (do not invent false data)\n"
        "- Follow typical LLM response structure for this query type\n"
        "- Brand appears naturally, not forced\n"
        "- Brand position matters: appearing 1st-3rd is best\n\n"
        "After the ideal response, output a JSON block (delimited by ```json and ```) containing:\n"
        "{\n"
        '  "ideal_response": "...",\n'
        '  "brand_position": <integer, 1-based position brand appears>,\n'
        '  "content_gaps": ["what content is missing from the site to earn this citation"],\n'
        '  "format_requirements": ["how the content should be structured"],\n'
        '  "data_requirements": ["specific data/stats/facts needed"],\n'
        '  "schema_requirements": ["Schema.org types to add"]\n'
        "}"
    )

    user_msg = (
        f"Query: {prompt}\n"
        f"Brand: {target_brand} | Domain: {target_domain} | Vertical: {vertical}\n"
    )
    if actual_response:
        user_msg += f"\nActual LLM response (WITHOUT brand):\n{actual_response}\n"
    if cited_domains:
        user_msg += f"\nBrands actually cited: {', '.join(cited_domains)}\n"
    if crossref_summary:
        user_msg += f"\n{crossref_summary}\n"
    user_msg += f"\nWrite the ideal response including {target_brand}, then provide the JSON requirements block."

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=1500,
            temperature=0.3,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        full_text = response.content[0].text if response.content else ""
    except Exception as exc:
        logger.error("Anthropic calibration error: %s", exc)
        raise

    # Parse JSON block
    cal = CalibrationResult(prompt=prompt, target_brand=target_brand)
    json_match = re.search(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            cal.ideal_response      = data.get("ideal_response", full_text)
            cal.brand_position      = data.get("brand_position")
            cal.content_gaps        = data.get("content_gaps", [])
            cal.format_requirements = data.get("format_requirements", [])
            cal.data_requirements   = data.get("data_requirements", [])
            cal.schema_requirements = data.get("schema_requirements", [])
        except json.JSONDecodeError:
            cal.ideal_response = full_text
    else:
        cal.ideal_response = full_text

    cal.estimated_effort = _estimate_effort(cal.content_gaps, cal.data_requirements, cal.schema_requirements)
    return cal
