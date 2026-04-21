"""
Sentiment analyzer for brand mentions in AI responses (Prompt 23).
Uses Claude claude-haiku-4-5-20251001 via Anthropic SDK. Costs ~$0.002/session.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

import anthropic

logger = logging.getLogger("sentiment_analyzer")

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = (
    "Analyze brand mentions in the AI response text. "
    "Return ONLY valid JSON with this exact schema: "
    '{"sentiment": "positive|neutral|negative|mixed", '
    '"confidence": 0.0-1.0, '
    '"brand_mentions": [{"text": "...", "sentiment": "positive|neutral|negative|mixed", '
    '"context_type": "recommendation|comparison|warning|neutral_mention|complaint"}], '
    '"summary": "one sentence"}'
)


@dataclass
class BrandMention:
    text: str
    sentiment: str       # positive | neutral | negative | mixed
    context_type: str    # recommendation | comparison | warning | neutral_mention | complaint


@dataclass
class SentimentResult:
    overall_sentiment: str   # positive | neutral | negative | mixed | not_mentioned
    confidence: float
    brand_mentions: List[BrandMention] = field(default_factory=list)
    summary: str = ""
    from_cache: bool = False

    def to_dict(self) -> dict:
        return {
            "overall_sentiment": self.overall_sentiment,
            "confidence": self.confidence,
            "brand_mentions": [
                {
                    "text": m.text,
                    "sentiment": m.sentiment,
                    "context_type": m.context_type,
                }
                for m in self.brand_mentions
            ],
            "summary": self.summary,
            "from_cache": self.from_cache,
        }


async def analyze_sentiment(
    ai_response_text: str,
    brand: str,
    target_domain: Optional[str] = None,
) -> SentimentResult:
    """
    Analyze the sentiment of brand mentions within an AI-generated response.

    Args:
        ai_response_text: The full text of the AI response to analyze.
        brand:            The brand name to look for (case-insensitive).
        target_domain:    Optional domain hint (unused in the prompt but available
                          for future context enrichment).

    Returns:
        SentimentResult with parsed sentiment data, or a safe fallback on error.
    """
    # Fast path: brand not mentioned at all.
    if brand.lower() not in ai_response_text.lower():
        return SentimentResult(
            overall_sentiment="not_mentioned",
            confidence=1.0,
            brand_mentions=[],
            summary="Brand not mentioned in AI response",
        )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    client = anthropic.AsyncAnthropic(api_key=api_key)

    user_message = f"Brand: {brand}\n\nAI Response:\n{ai_response_text[:3000]}"

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=500,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error during sentiment analysis: {e}")
        return SentimentResult(
            overall_sentiment="neutral",
            confidence=0.5,
            brand_mentions=[],
            summary=f"Analysis failed: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Unexpected error during sentiment analysis: {e}")
        return SentimentResult(
            overall_sentiment="neutral",
            confidence=0.5,
            brand_mentions=[],
            summary=f"Analysis failed: {str(e)}",
        )

    # Parse the JSON response from the model.
    try:
        raw_text = response.content[0].text.strip()
        data = json.loads(raw_text)

        brand_mentions = [
            BrandMention(
                text=m.get("text", ""),
                sentiment=m.get("sentiment", "neutral"),
                context_type=m.get("context_type", "neutral_mention"),
            )
            for m in data.get("brand_mentions", [])
        ]

        return SentimentResult(
            overall_sentiment=data.get("sentiment", "neutral"),
            confidence=float(data.get("confidence", 0.5)),
            brand_mentions=brand_mentions,
            summary=data.get("summary", ""),
        )

    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        logger.error(f"Failed to parse sentiment analysis response: {e}")
        return SentimentResult(
            overall_sentiment="neutral",
            confidence=0.5,
            brand_mentions=[],
            summary=f"Analysis failed: {str(e)}",
        )
