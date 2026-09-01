"""
Unit tests for core.direct_analyzer.AsyncLLMClient -- specifically the
Perplexity provider support added in Etapa 2.2 of the consolidation
(docs/CONSOLIDATION_PLAN.md), needed so api/routes/citation_tracker.py (and
other files with their own duplicated Perplexity client instantiation) can
be migrated onto the single shared client instead of each maintaining their
own AsyncOpenAI(base_url=...) construction.

No live API calls here -- these check client construction and the
provider dispatch, not actual completions (see Etapa 2.1's tests for how
this project validates live API behavior before relying on it).
"""
import os
import unittest
from unittest.mock import patch

from core.direct_analyzer import AsyncLLMClient


class TestPerplexityClientConstruction(unittest.TestCase):
    @patch.dict(os.environ, {"PERPLEXITY_API_KEY": "test-key-123"})
    def test_perplexity_uses_openai_sdk_with_correct_base_url(self):
        client = AsyncLLMClient(provider="PERPLEXITY", model_name="sonar")
        # Perplexity's API is OpenAI-compatible -- same SDK, different endpoint.
        self.assertEqual(str(client._client.base_url).rstrip("/"), "https://api.perplexity.ai")

    def test_unknown_provider_still_raises(self):
        with self.assertRaises(ValueError):
            AsyncLLMClient(provider="NOT_A_REAL_PROVIDER", model_name="x")


class TestExistingProvidersUnaffected(unittest.TestCase):
    """Adding Perplexity must not change behavior for the four providers
    that were already supported."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})
    def test_anthropic_still_constructs(self):
        client = AsyncLLMClient(provider="ANTHROPIC", model_name="claude-sonnet-4-6")
        self.assertEqual(client.provider, "ANTHROPIC")

    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
    def test_openai_still_constructs(self):
        client = AsyncLLMClient(provider="OPENAI", model_name="gpt-4o")
        self.assertEqual(client.provider, "OPENAI")
        # OpenAI's own base_url must be untouched by the Perplexity branch.
        self.assertNotIn("perplexity", str(client._client.base_url))


if __name__ == "__main__":
    unittest.main()
