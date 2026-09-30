"""
Shared embeddings infrastructure -- Pasul 14/17 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Built once here (Pasul 14: internal link suggestions need page-to-page
topical similarity), reused unchanged by Pasul 17 (Fan-Out sub-query
coverage needs query-to-passage similarity) -- per the plan's explicit
instruction not to duplicate this.

OpenAI text-embedding-3-small: cheap ($0.02 / 1M tokens as of writing), and
this project already has OPENAI_API_KEY configured. Every real call is
cost-tracked via api.routes.costs.track_cost, the same as every other paid
call in this project (CLAUDE.md rule 4). Cached in text_embeddings by
(sha256 of the exact text, model) so the same passage is never paid for
twice, whichever feature asks for it first.
"""
import hashlib
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_PROVIDER = "openai"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _load_cached(hash_: str, model: str) -> Optional[List[float]]:
    from sqlalchemy import select

    from api.models._base import AsyncSessionLocal
    from api.models.database import TextEmbedding

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(TextEmbedding).where(
                TextEmbedding.content_hash == hash_, TextEmbedding.model == model,
            )
        )).scalar_one_or_none()
        return row.vector if row else None


async def _store(hash_: str, model: str, vector: List[float]) -> None:
    from api.models._base import AsyncSessionLocal
    from api.models.database import TextEmbedding

    try:
        async with AsyncSessionLocal() as db:
            db.add(TextEmbedding(content_hash=hash_, model=model, vector=vector))
            await db.commit()
    except Exception as exc:
        # A unique-constraint race (two concurrent callers embedding the
        # same text) or any other write failure must never fail the
        # caller's actual result over a caching problem.
        logger.warning("Could not cache embedding: %s", exc)


async def embed_text(text: str, model: str = EMBEDDING_MODEL, source: str = "embeddings") -> List[float]:
    """
    One text's embedding vector: from cache if this exact text was already
    embedded with this model, otherwise a real (paid) OpenAI call, cached
    for next time.
    """
    hash_ = content_hash(text)
    cached = await _load_cached(hash_, model)
    if cached is not None:
        return cached

    import openai

    from api.routes.costs import track_cost

    client = openai.AsyncOpenAI()
    try:
        response = await client.embeddings.create(model=model, input=text)
    except openai.APIError as exc:
        logger.error("Embedding request failed: %s", exc)
        raise

    vector = response.data[0].embedding
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "total_tokens", None) or getattr(usage, "prompt_tokens", None) or 0
    await track_cost(
        source=source, provider=EMBEDDING_PROVIDER, model=model,
        input_tokens=input_tokens, output_tokens=0,
    )

    await _store(hash_, model, vector)
    return vector


async def embed_texts(texts: List[str], model: str = EMBEDDING_MODEL, source: str = "embeddings") -> List[List[float]]:
    """
    Embed several texts. One call per text (not one batched API call) so a
    partial cache hit doesn't force re-embedding texts already cached --
    cost-negligible at this model's price, and simpler to reason about.
    """
    return [await embed_text(t, model=model, source=source) for t in texts]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """0.0 when either vector is degenerate (all zeros) rather than raising."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
