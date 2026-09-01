"""
Unit tests for core.output_schemas — the JSON Schemas used with Anthropic
structured outputs (output_config.format) to replace prompt-based JSON
instructions + best-effort repair for audit types that have been migrated
(docs/CONSOLIDATION_PLAN.md Etapa 2.1).

These are structural sanity checks, not a live API test. A live call against
the real Anthropic API (verifying the schema is actually accepted and
produces sensible output) was run manually during development — see the
Etapa 2.1 commit message. Anthropic's structured-output schema support
rejects `minimum`/`maximum` on integer types (confirmed via a live 400
response), which is why score fields here are plain {"type": "integer"}.
"""
import unittest

from core.output_schemas import AUDIT_OUTPUT_SCHEMAS, GEO_AUDIT_SCHEMA, get_output_schema


def _walk_schema_nodes(node):
    """Yield every dict node in a JSON Schema tree."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_schema_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_schema_nodes(item)


class TestGetOutputSchema(unittest.TestCase):
    def test_migrated_type_returns_schema(self):
        schema = get_output_schema("GEO_AUDIT")
        self.assertIsNotNone(schema)
        self.assertEqual(schema, GEO_AUDIT_SCHEMA)

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(get_output_schema("geo_audit"), GEO_AUDIT_SCHEMA)
        self.assertEqual(get_output_schema("Geo_Audit"), GEO_AUDIT_SCHEMA)

    def test_unmigrated_type_returns_none(self):
        """Every other audit type must fall back to the pre-existing
        prompt-instructions + clean_json_response path unchanged."""
        self.assertIsNone(get_output_schema("SEO_AUDIT"))
        self.assertIsNone(get_output_schema("CONTENT_QUALITY"))
        self.assertIsNone(get_output_schema("NOT_A_REAL_AUDIT_TYPE"))


class TestGeoAuditSchemaShape(unittest.TestCase):
    def test_top_level_matches_prompts_geo_audit_yaml(self):
        """Mirrors prompts/geo_audit.yaml's output_schema exactly -- keep in
        sync by hand if that YAML changes (docs/CONSOLIDATION_PLAN.md Etapa 2.1)."""
        self.assertEqual(
            set(GEO_AUDIT_SCHEMA["properties"].keys()),
            {"geo_audit", "issues", "recommendations", "geo_score_justification"},
        )
        self.assertEqual(set(GEO_AUDIT_SCHEMA["required"]), set(GEO_AUDIT_SCHEMA["properties"].keys()))

    def test_no_unsupported_integer_constraints(self):
        """Anthropic rejects minimum/maximum on integer-typed schema
        properties (verified live, HTTP 400 invalid_request_error)."""
        for node in _walk_schema_nodes(GEO_AUDIT_SCHEMA):
            if node.get("type") == "integer":
                self.assertNotIn("minimum", node)
                self.assertNotIn("maximum", node)

    def test_every_object_node_is_closed(self):
        """additionalProperties: False everywhere an object is defined --
        required for Anthropic's strict structured-output validation."""
        for node in _walk_schema_nodes(GEO_AUDIT_SCHEMA):
            if node.get("type") == "object":
                self.assertIn("additionalProperties", node)
                self.assertFalse(node["additionalProperties"])

    def test_platform_suitability_fields(self):
        platform = GEO_AUDIT_SCHEMA["properties"]["geo_audit"]["properties"]["platform_suitability"]
        self.assertEqual(
            set(platform["properties"].keys()),
            {"perplexity", "google_aio", "chatgpt_browse", "bing_copilot", "claude_gemini"},
        )


if __name__ == "__main__":
    unittest.main()
