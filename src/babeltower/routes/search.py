from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import SIGNED_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.metrics import searches_total
from babeltower.models import Agent
from babeltower.rate_limit import limiter
from babeltower.routes.intents import get_redis
from babeltower.schemas import SearchCandidate, SearchRequest, SearchResponse

from .intents import EMBEDDER_DEPENDENCY

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)
REDIS_DEPENDENCY = Depends(get_redis)
SIMILARITY_THRESHOLD = 0.70


def _embedding_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(value) for value in embedding) + "]"


async def search_intents(
    session: AsyncSession,
    agent: Agent,
    body: SearchRequest,
    query_embedding: list[float],
) -> list[SearchCandidate]:
    if hasattr(session, "search_intents"):
        return await session.search_intents(  # type: ignore[attr-defined]
            agent=agent,
            body=body,
            query_embedding=query_embedding,
        )

    params: dict[str, Any] = {
        "query_emb": _embedding_literal(query_embedding),
        "requester_agent_id": agent.id,
        "threshold": SIMILARITY_THRESHOLD,
        "limit": body.max_results,
    }
    match_type_sql = ""
    if body.query_intent.match_type is not None:
        params["match_type"] = body.query_intent.match_type
        match_type_sql = " AND i.match_type = :match_type"

    filter_clauses = []
    for index, (key, value) in enumerate(body.query_intent.filters.items()):
        key_param = f"filter_key_{index}"
        value_param = f"filter_value_{index}"
        filter_clauses.append(f"(i.filters ->> :{key_param}) = :{value_param}")
        params[key_param] = key
        params[value_param] = str(value)

    filters_sql = ""
    if filter_clauses:
        filters_sql = " AND " + " AND ".join(filter_clauses)

    query = text(
        f"""
        SELECT
            i.id AS intent_id,
            a.pubkey AS agent_pubkey,
            i.match_type,
            i.seeking,
            i.offering,
            i.constraints,
            i.filters,
            1 - (i.embedding <=> CAST(:query_emb AS halfvec(1024))) AS similarity,
            a.status AS agent_status
        FROM intents i
        JOIN agents a ON a.id = i.agent_id
        WHERE i.status = 'active'
          {match_type_sql}
          AND i.agent_id != :requester_agent_id
          AND NOT EXISTS (
            SELECT 1 FROM blocks b
            WHERE (b.blocker_id = :requester_agent_id AND b.blocked_id = i.agent_id)
               OR (b.blocker_id = i.agent_id AND b.blocked_id = :requester_agent_id)
          )
          {filters_sql}
          AND 1 - (i.embedding <=> CAST(:query_emb AS halfvec(1024))) >= :threshold
        ORDER BY i.embedding <=> CAST(:query_emb AS halfvec(1024))
        LIMIT :limit
        """
    )
    result = await session.execute(query, params)
    return [SearchCandidate(**dict(row._mapping)) for row in result]


@router.post("/search", response_model=SearchResponse)
@limiter.limit("10/minute")
async def search(
    request: Request,
    body: SearchRequest,
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
    redis: Any = REDIS_DEPENDENCY,
    embedder=EMBEDDER_DEPENDENCY,
) -> SearchResponse:
    del request
    query = body.query_intent
    query_embedding = await embedder(
        query.seeking,
        query.offering,
        query.constraints,
        redis=redis,
        input_type="query",
    )
    candidates = await search_intents(session, agent, body, query_embedding)
    searches_total.inc()
    return SearchResponse(candidates=candidates)
