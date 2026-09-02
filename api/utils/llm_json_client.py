"""
Shared LLM-call + JSON-repair helpers, extracted from api/routes/summary.py
(Etapa 5.1 of the consolidation, docs/CONSOLIDATION_PLAN.md).

call_llm_for_summary() and clean_json_response() were defined in summary.py
but imported by 8 other files across the codebase (action_cards.py,
content_briefs.py, benchmarks.py, content_gaps.py, draft_optimizer.py,
gap_analysis.py, schedules.py, plus summary.py itself) -- load-bearing
shared infrastructure that happened to live inside one router module rather
than a real shared location. api/routes/summary.py re-exports these names
so none of those 8 import sites needed to change.
"""

import json
import os
import re

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mistralai import Mistral


def _repair_json(text: str) -> str:
    """
    Attempt to repair common LLM JSON issues:
      - Trailing commas before } or ]
      - Missing commas between adjacent string values
      - Unterminated strings (truncated output): closes open structures gracefully
    """
    # Fix trailing commas before closing bracket/brace
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    # Fix missing commas between "value"\n"key": patterns
    text = re.sub(r'("(?:[^"\\]|\\.)*")\s*\n(\s*")', r'\1,\n\2', text)

    # Try parsing; if it still fails with an unterminated string, truncate & close
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError as e:
        err = str(e)
        if 'Unterminated string' in err or 'Expecting' in err:
            # Truncate to just before the bad position and close open structures
            pos = getattr(e, 'pos', len(text))
            truncated = text[:pos].rstrip().rstrip(',')
            # Walk through and track bracket/brace depth (skip strings)
            stack = []
            in_string = False
            escape_next = False
            for ch in truncated:
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch in '{[':
                        stack.append(ch)
                    elif ch in '}]' and stack:
                        stack.pop()
            # If we're still inside a string, close it first
            if in_string:
                truncated += '"'
            # Close all open structures
            closing = ''.join('}' if c == '{' else ']' for c in reversed(stack))
            return truncated + closing
    return text


def clean_json_response(text: str) -> str:
    """Strip markdown code fences and repair common JSON issues from LLM responses."""
    text = text.strip()
    # Remove ```json or ``` prefix
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    # Remove trailing ```
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    text = text.strip()
    # Attempt structural repair
    text = _repair_json(text)
    return text


async def call_llm_for_summary(
    provider: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4096
) -> tuple[str, int, int]:
    """
    Call LLM provider to generate a free-text (JSON-shaped) response.

    Returns:
        (raw_response_text, input_tokens, output_tokens)
    """
    provider = provider.upper()

    if provider == "ANTHROPIC":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")

        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )
        in_tok  = response.usage.input_tokens  if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        return response.content[0].text, in_tok, out_tok

    elif provider == "OPENAI":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"}
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok

    elif provider == "MISTRAL":
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY not configured")

        client = Mistral(api_key=api_key)
        response = await client.chat.complete_async(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"}
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok

    elif provider == "GOOGLE":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not configured")

        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                temperature=0.3,
                response_mime_type="application/json"
            )
        )
        meta = getattr(response, "usage_metadata", None)
        in_tok  = getattr(meta, "prompt_token_count",     0) if meta else 0
        out_tok = getattr(meta, "candidates_token_count", 0) if meta else 0
        return response.text, in_tok, out_tok

    else:
        raise ValueError(f"Unknown provider: {provider}")
