"""
Pins the Anthropic model migration from Etapa 2.3 of the consolidation
(docs/CONSOLIDATION_PLAN.md): claude-sonnet-4-20250514 (deprecated by
Anthropic 2026-09) -> claude-sonnet-4-6 as the default "balanced" model.

The old model ID must stay resolvable for cost lookups — 5,000+ historical
cost_records rows reference it by exact string — so this also guards
against someone "cleaning up" the legacy catalog entry later without
realizing why it's there.
"""
import unittest

from api.provider_registry import get_default_model, get_model, ALL_MODELS


class TestAnthropicDefaultModel(unittest.TestCase):
    def test_default_model_is_not_the_deprecated_one(self):
        self.assertEqual(get_default_model("anthropic"), "claude-sonnet-4-6")
        self.assertNotEqual(get_default_model("anthropic"), "claude-sonnet-4-20250514")

    def test_new_model_is_registered_with_a_price(self):
        model = get_model("claude-sonnet-4-6")
        self.assertIsNotNone(model)
        self.assertEqual(model.provider, "anthropic")
        self.assertGreater(model.input_price, 0)
        self.assertGreater(model.output_price, 0)

    def test_deprecated_model_still_resolves_for_historical_cost_lookups(self):
        """Must not be removed from ALL_MODELS -- see F4-04 in docs/audit/04-integrations.md."""
        model = get_model("claude-sonnet-4-20250514")
        self.assertIsNotNone(model)
        self.assertEqual(model.input_price, 3.00)
        self.assertEqual(model.output_price, 15.00)

    def test_no_other_call_site_regressed_to_the_deprecated_id(self):
        """Belt-and-suspenders: the deprecated ID should appear in ALL_MODELS
        exactly once (the legacy entry itself), not spread back into new code."""
        deprecated_entries = [m for m in ALL_MODELS if m.id == "claude-sonnet-4-20250514"]
        self.assertEqual(len(deprecated_entries), 1)


if __name__ == "__main__":
    unittest.main()
