"""
Pasul 14/17 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.embeddings, with the real OpenAI call mocked -- caching against the
real test database (never the production one; tests/conftest.py points
GEO_TOOL_DB_PATH at a temp file).
"""
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import delete, select

from core.embeddings import content_hash, cosine_similarity, embed_text, embed_texts


def _fake_openai_response(vector):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=vector)]
    resp.usage = MagicMock(total_tokens=7)
    return resp


class TestContentHash(unittest.TestCase):
    def test_same_text_hashes_the_same_way(self):
        self.assertEqual(content_hash("hello"), content_hash("hello"))

    def test_different_text_hashes_differently(self):
        self.assertNotEqual(content_hash("hello"), content_hash("world"))


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors_are_maximally_similar(self):
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal_vectors_are_zero_similarity(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_opposite_vectors_are_negatively_similar(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)

    def test_a_zero_vector_is_zero_similarity_not_an_error(self):
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)

    def test_empty_or_mismatched_vectors_are_zero_not_an_error(self):
        self.assertEqual(cosine_similarity([], []), 0.0)
        self.assertEqual(cosine_similarity([1, 2], [1, 2, 3]), 0.0)


class TestEmbedText(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        from api.models._base import AsyncSessionLocal
        from api.models.database import TextEmbedding
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TextEmbedding).where(TextEmbedding.model == "test-model"))
            await db.commit()

    async def test_a_fresh_text_calls_openai_and_caches_the_result(self):
        text = f"unique text {uuid.uuid4()}"
        vector = [0.1, 0.2, 0.3]
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=_fake_openai_response(vector))

        with patch("openai.AsyncOpenAI", return_value=mock_client), \
             patch("api.routes.costs.track_cost", AsyncMock()) as track_cost:
            result = await embed_text(text, model="test-model")

        self.assertEqual(result, vector)
        mock_client.embeddings.create.assert_awaited_once()
        track_cost.assert_awaited_once()

    async def test_the_same_text_twice_only_calls_openai_once(self):
        text = f"repeated text {uuid.uuid4()}"
        vector = [0.4, 0.5, 0.6]
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=_fake_openai_response(vector))

        with patch("openai.AsyncOpenAI", return_value=mock_client), \
             patch("api.routes.costs.track_cost", AsyncMock()):
            first = await embed_text(text, model="test-model")
            second = await embed_text(text, model="test-model")

        self.assertEqual(first, second)
        mock_client.embeddings.create.assert_awaited_once()   # not twice

    async def test_a_failed_openai_call_propagates_not_swallowed(self):
        import openai

        text = f"will fail {uuid.uuid4()}"
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=openai.APIError("boom", request=MagicMock(), body=None))

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with self.assertRaises(openai.APIError):
                await embed_text(text, model="test-model")

    async def test_embed_texts_embeds_each_one(self):
        vectors = [[1.0], [2.0], [3.0]]
        mock_client = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=[_fake_openai_response(v) for v in vectors])

        texts = [f"text {i} {uuid.uuid4()}" for i in range(3)]
        with patch("openai.AsyncOpenAI", return_value=mock_client), \
             patch("api.routes.costs.track_cost", AsyncMock()):
            results = await embed_texts(texts, model="test-model")

        self.assertEqual(results, vectors)


if __name__ == "__main__":
    unittest.main()
