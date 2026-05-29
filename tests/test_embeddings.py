import os

import pytest

from babeltower import embeddings
from babeltower.embeddings import EMBEDDING_DIMENSIONS, embed_intent, intent_embedding_text


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value


def test_intent_embedding_text_uses_protocol_separator():
    assert intent_embedding_text("seek", "offer", "constraints") == "seek\n\noffer\n\nconstraints"
    assert intent_embedding_text("seek", "offer", "") == "seek\n\noffer"


async def test_embed_intent_caches_identical_inputs(monkeypatch):
    calls = 0

    async def fake_voyage(text, input_type="document"):
        nonlocal calls
        calls += 1
        return [0.1] * EMBEDDING_DIMENSIONS

    monkeypatch.setattr(embeddings, "_embed_with_voyage", fake_voyage)
    redis = FakeRedis()

    first = await embed_intent("seek", "offer", "constraints", redis=redis)
    second = await embed_intent("seek", "offer", "constraints", redis=redis)

    assert first == second == [0.1] * EMBEDDING_DIMENSIONS
    assert calls == 1


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1" or not os.getenv("VOYAGE_API_KEY"),
    reason="live Voyage smoke test requires RUN_LIVE_TESTS=1 and VOYAGE_API_KEY",
)
async def test_embed_intent_live_voyage_smoke():
    embedding = await embed_intent(
        "technical co-founder for biotech startup",
        "ML engineer with biotech background",
        "Seoul or remote",
    )

    assert len(embedding) == EMBEDDING_DIMENSIONS

