"""
JSON Schemas for Anthropic structured outputs (output_config.format), used to
replace prompt-based "return JSON like this" instructions + best-effort
repair (clean_json_response) with an API-enforced schema for audit types
that have been migrated.

Migration status (docs/CONSOLIDATION_PLAN.md Etapa 2.1): only GEO_AUDIT is
wired in so far, validated against prompts/geo_audit.yaml's output_schema
before expanding to other audit types. Each entry here MUST be kept in sync
with the corresponding prompts/*.yaml output_schema by hand — there is no
automatic conversion from the YAML's prose description to a JSON Schema.
"""
from typing import Optional

# ---------------------------------------------------------------------------
# GEO_AUDIT — mirrors prompts/geo_audit.yaml output_schema exactly.
# ---------------------------------------------------------------------------

# NOTE: Anthropic's structured-output JSON Schema support rejects
# minimum/maximum constraints on integer types (verified live: 400
# invalid_request_error). The 0-100 range is enforced by the prompt's
# own instructions instead (prompts/geo_audit.yaml), not the schema.
_SCORE_INT = {"type": "integer"}

GEO_AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "geo_audit": {
            "type": "object",
            "properties": {
                "overall_score": _SCORE_INT,
                "classification": {
                    "type": "string",
                    "enum": ["non-citable", "weak", "average", "strong", "exceptional"],
                },
                "citation_probability": _SCORE_INT,
                "information_gain_score": _SCORE_INT,
                "authority_score": _SCORE_INT,
                "structure_score": _SCORE_INT,
                "factual_density_score": _SCORE_INT,
                "anti_hallucination_score": _SCORE_INT,
                "detected_language": {"type": "string"},
                "content_type": {
                    "type": "string",
                    "enum": ["factual", "educational", "mixed", "promotional", "navigational"],
                },
                "would_perplexity_cite": {"type": "string", "enum": ["yes", "unlikely", "no"]},
                "information_gain": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                "platform_suitability": {
                    "type": "object",
                    "properties": {
                        "perplexity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "google_aio": {"type": "string", "enum": ["high", "medium", "low"]},
                        "chatgpt_browse": {"type": "string", "enum": ["high", "medium", "low"]},
                        "bing_copilot": {"type": "string", "enum": ["high", "medium", "low"]},
                        "claude_gemini": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": [
                        "perplexity", "google_aio", "chatgpt_browse", "bing_copilot", "claude_gemini",
                    ],
                    "additionalProperties": False,
                },
                "quotable_statements": {"type": "array", "items": {"type": "string"}},
                "entities_detected": {"type": "array", "items": {"type": "string"}},
                "statistics_found": {"type": "array", "items": {"type": "string"}},
                "schema_signals_detected": {"type": "array", "items": {"type": "string"}},
                "hedging_phrases_detected": {"type": "array", "items": {"type": "string"}},
                "unique_data_points": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "overall_score", "classification", "citation_probability",
                "information_gain_score", "authority_score", "structure_score",
                "factual_density_score", "anti_hallucination_score", "detected_language",
                "content_type", "would_perplexity_cite", "information_gain",
                "platform_suitability", "quotable_statements", "entities_detected",
                "statistics_found", "schema_signals_detected", "hedging_phrases_detected",
                "unique_data_points",
            ],
            "additionalProperties": False,
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "citation", "structure", "authority", "entity", "freshness",
                            "information_gain", "anti_hallucination", "platform",
                        ],
                    },
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "finding": {"type": "string"},
                    "impact": {"type": "string"},
                },
                "required": ["category", "severity", "finding", "impact"],
                "additionalProperties": False,
            },
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {
                        "type": "string",
                        "enum": [
                            "citation", "structure", "authority", "entity", "schema",
                            "information_gain", "anti_hallucination",
                        ],
                    },
                    "recommendation": {"type": "string"},
                    "before_example": {"type": "string"},
                    "after_example": {"type": "string"},
                    "expected_impact": {"type": "string"},
                },
                "required": [
                    "priority", "category", "recommendation",
                    "before_example", "after_example", "expected_impact",
                ],
                "additionalProperties": False,
            },
        },
        "geo_score_justification": {"type": "string"},
    },
    "required": ["geo_audit", "issues", "recommendations", "geo_score_justification"],
    "additionalProperties": False,
}


# Maps AUDIT_TYPE (uppercase, matches prompts/*.yaml basename) -> JSON Schema.
# Empty/missing entry means: no structured-output schema yet, caller falls
# back to prompt instructions + clean_json_response repair as before.
AUDIT_OUTPUT_SCHEMAS: dict = {
    "GEO_AUDIT": GEO_AUDIT_SCHEMA,
}


def get_output_schema(audit_type: str) -> Optional[dict]:
    """Return the JSON Schema for this audit type, or None if not yet migrated."""
    return AUDIT_OUTPUT_SCHEMAS.get(audit_type.upper())
