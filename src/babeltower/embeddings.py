from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Optional

from babeltower.config import get_settings

# voyage-4-lite: current-gen lite tier at $0.02/M tokens (same as voyage-3),
# 1024-dim default (no schema change), 32K context, MRL-truncatable to
# 256/512/2048 if we ever want a smaller index. All voyage-4-series
# embeddings are cross-compatible per Voyage's docs, so this can be
# upgraded to voyage-4 or voyage-4-large later without re-embedding.
EMBEDDING_MODEL = "voyage-4-lite"
EMBEDDING_DIMENSIONS = 1024
EMBEDDING_CACHE_TTL_SECONDS = 3600


def intent_embedding_text(seeking: str, offering: str, constraints: str = "") -> str:
    return f"{seeking}\n\n{offering}\n\n{constraints}".strip()


def _cache_key(text: str) -> str:
    return f"emb:{hashlib.sha256(text.encode()).hexdigest()}"


async def _redis_get(redis: Any, key: str) -> Optional[list[float]]:
    if redis is None:
        return None
    cached = await redis.get(key)
    if cached is None:
        return None
    return json.loads(cached)


async def _redis_set(redis: Any, key: str, embedding: list[float]) -> None:
    if redis is not None:
        await redis.setex(key, EMBEDDING_CACHE_TTL_SECONDS, json.dumps(embedding))


async def _embed_with_voyage(text: str) -> list[float]:
    import voyageai

    settings = get_settings()
    client = voyageai.Client(api_key=settings.voyage_api_key, max_retries=0, timeout=30)

    def call_voyage() -> list[float]:
        result = client.embed(
            [text],
            model=EMBEDDING_MODEL,
            input_type="document",
            output_dimension=EMBEDDING_DIMENSIONS,
        )
        return list(result.embeddings[0])

    return await asyncio.to_thread(call_voyage)


async def embed_intent(
    seeking: str,
    offering: str,
    constraints: str = "",
    *,
    redis: Any = None,
) -> list[float]:
    text = intent_embedding_text(seeking, offering, constraints)
    key = _cache_key(text)
    cached = await _redis_get(redis, key)
    if cached is not None:
        return cached

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            embedding = await _embed_with_voyage(text)
            if len(embedding) != EMBEDDING_DIMENSIONS:
                raise ValueError("voyage returned unexpected embedding dimension")
            await _redis_set(redis, key, embedding)
            return embedding
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(0.25 * (2**attempt))

    raise RuntimeError("embedding request failed") from last_error
