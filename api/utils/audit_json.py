"""
Shared knowledge about the shape of an audit's stored result_json.

Etapa 5.2 of the consolidation (docs/CONSOLIDATION_PLAN.md). Used by both
api/routes/compare.py (_extract_criteria_averages, for cross-audit
comparison) and api/routes/audit_rerun.py (score extraction after
re-analyzing a single page) -- previously two independent, divergent
copies of the same list lived inside compare.py, causing the same
audit_type's score to extract successfully in one function and silently
come back empty in the other.
"""

# Well-known top-level wrapper keys used by our audit prompts' output JSON,
# e.g. { "seo_audit": { "overall_score": 75, "title_tag": {"score": 80, ...} } }
AUDIT_ROOT_KEYS = [
    "seo_audit", "geo_audit", "internal_linking_audit", "internal_linking",
    "content_brief_audit", "accessibility_audit", "gdpr_audit",
    "social_media_audit", "page_speed_audit", "schema_markup_audit",
    "content_quality_audit", "content_quality", "technical_seo_audit",
    "competitor_analysis", "brand_sentiment_audit", "mobile_audit",
    "ux_content_audit", "brand_voice_audit", "ecommerce_audit",
    "translation_audit", "competitive_positioning_audit",
    "spelling_grammar_audit", "readability_audit", "freshness_audit",
    "local_seo_audit", "security_content_audit", "ai_overview_audit",
]
