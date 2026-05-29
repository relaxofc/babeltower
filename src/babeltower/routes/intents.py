from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import MUTATION_AGENT_DEPENDENCY, SIGNED_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.embeddings import embed_intent
from babeltower.metrics import intents_created_total
from babeltower.models import Agent, ConnectionRequest, Intent
from babeltower.moderation import check_and_apply_soft_ban, scan_intent_text
from babeltower.schemas import IntentCreateRequest, IntentResponse, OwnedIntentsResponse

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)
MATCH_TYPE_RE = re.compile(r"^[a-z0-9-]{1,64}$")


async def get_redis(request: Request):
    return getattr(request.app.state, "redis", None)


async def get_embedder():
    return embed_intent


REDIS_DEPENDENCY = Depends(get_redis)
EMBEDDER_DEPENDENCY = Depends(get_embedder)


def _now() -> datetime:
    return datetime.now(UTC)


def _to_response(intent: Intent, agent_pubkey: str) -> IntentResponse:
    return IntentResponse(
        intent_id=intent.id,
        agent_pubkey=agent_pubkey,
        match_type=intent.match_type,
        seeking=intent.seeking,
        offering=intent.offering,
        constraints=intent.constraints,
        filters=intent.filters,
        ttl_days=intent.ttl_days,
        created_at=intent.created_at,
        expires_at=intent.expires_at,
        status=intent.status,
    )


def _blocked_response(violations: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error_code": "content_blocked", "violations": violations},
    )


def _error_response(status_code: int, error_code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error_code": error_code})


def _is_valid_match_type(match_type: str) -> bool:
    return bool(MATCH_TYPE_RE.fullmatch(match_type))


async def count_active_intents(session: AsyncSession, agent_id: str) -> int:
    if hasattr(session, "count_active_intents"):
        return await session.count_active_intents(agent_id)  # type: ignore[attr-defined]

    return int(
        await session.scalar(
            select(func.count())
            .select_from(Intent)
            .where(Intent.agent_id == agent_id, Intent.status == "active")
        )
        or 0
    )


async def count_intents_created_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
    if hasattr(session, "count_intents_created_since"):
        return await session.count_intents_created_since(  # type: ignore[attr-defined]
            agent_id,
            since,
        )

    return int(
        await session.scalar(
            select(func.count())
            .select_from(Intent)
            .where(Intent.agent_id == agent_id, Intent.created_at >= since)
        )
        or 0
    )


async def persist_intent(
    session: AsyncSession,
    agent: Agent,
    body: IntentCreateRequest,
    embedding: list[float],
    created_at: datetime,
) -> Intent:
    if hasattr(session, "create_intent"):
        return await session.create_intent(  # type: ignore[attr-defined]
            agent=agent,
            body=body,
            embedding=embedding,
            created_at=created_at,
        )

    intent = Intent(
        agent_id=agent.id,
        match_type=body.match_type,
        seeking=body.seeking,
        offering=body.offering,
        constraints=body.constraints,
        filters=body.filters,
        embedding=embedding,
        ttl_days=body.ttl_days,
        created_at=created_at,
        expires_at=created_at + timedelta(days=body.ttl_days),
        status="active",
    )
    agent.intents_created_total = (agent.intents_created_total or 0) + 1
    session.add(intent)
    await session.commit()
    await session.refresh(intent)
    return intent


async def get_visible_intent(
    session: AsyncSession,
    agent: Agent,
    intent_id: str,
) -> Optional[Intent]:
    if hasattr(session, "get_visible_intent"):
        return await session.get_visible_intent(agent, intent_id)  # type: ignore[attr-defined]

    intent = await session.get(Intent, intent_id)
    if intent is None:
        return None
    if intent.agent_id == agent.id:
        return intent

    result = await session.execute(
        select(ConnectionRequest.id).where(
            or_(
                ConnectionRequest.target_intent_id == intent_id,
                ConnectionRequest.from_intent_id == intent_id,
            ),
            or_(
                ConnectionRequest.from_agent_id == agent.id,
                ConnectionRequest.to_agent_id == agent.id,
            ),
            ConnectionRequest.status.in_(("pending", "accepted")),
        )
    )
    if result.scalar_one_or_none() is not None:
        return intent
    return None


async def get_owned_intent(
    session: AsyncSession,
    agent: Agent,
    intent_id: str,
) -> Optional[Intent]:
    if hasattr(session, "get_owned_intent"):
        return await session.get_owned_intent(agent, intent_id)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Intent).where(Intent.id == intent_id, Intent.agent_id == agent.id)
    )
    return result.scalar_one_or_none()


async def list_reusable_owned_intents(session: AsyncSession, agent: Agent) -> list[Intent]:
    if hasattr(session, "list_reusable_owned_intents"):
        return await session.list_reusable_owned_intents(agent)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Intent)
        .where(
            Intent.agent_id == agent.id,
            Intent.status.in_(("active", "dormant")),
        )
        .order_by(Intent.created_at.desc())
    )
    return list(result.scalars())


async def soft_delete_intent(session: AsyncSession, intent: Intent) -> None:
    if hasattr(session, "delete_intent"):
        await session.delete_intent(intent)  # type: ignore[attr-defined]
        return

    intent.status = "deleted"
    await session.commit()


async def refresh_owned_intent(
    session: AsyncSession,
    intent: Intent,
    refreshed_at: datetime,
) -> Intent:
    if hasattr(session, "refresh_intent"):
        return await session.refresh_intent(intent, refreshed_at)  # type: ignore[attr-defined]

    intent.status = "active" if intent.status == "dormant" else intent.status
    intent.expires_at = refreshed_at + timedelta(days=intent.ttl_days)
    await session.commit()
    await session.refresh(intent)
    return intent


@router.post("/intents", response_model=IntentResponse, status_code=status.HTTP_201_CREATED)
async def create_intent(
    body: IntentCreateRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
    redis: Any = REDIS_DEPENDENCY,
    embedder=EMBEDDER_DEPENDENCY,
):
    if not _is_valid_match_type(body.match_type):
        return _error_response(400, "invalid_match_type")

    violations = scan_intent_text(f"{body.seeking}\n{body.offering}\n{body.constraints}")
    if violations:
        return _blocked_response(violations)

    active_count = await count_active_intents(session, agent.id)
    if active_count >= 10:
        return _error_response(409, "intent_limit_reached")

    created_at = _now()
    daily_count = await count_intents_created_since(
        session,
        agent.id,
        created_at - timedelta(days=1),
    )
    if daily_count >= 30:
        return _error_response(429, "rate_limited")

    embedding = await embedder(
        body.seeking,
        body.offering,
        body.constraints,
        redis=redis,
    )
    intent = await persist_intent(session, agent, body, embedding, created_at)
    await check_and_apply_soft_ban(session, agent.id, created_at)
    if hasattr(session, "commit"):
        await session.commit()
    intents_created_total.inc()
    return _to_response(intent, agent.pubkey)


async def get_agent_pubkey_by_id(session: AsyncSession, agent_id: str) -> Optional[str]:
    if hasattr(session, "get_agent_pubkey_by_id"):
        return await session.get_agent_pubkey_by_id(agent_id)  # type: ignore[attr-defined]
    result = await session.execute(select(Agent.pubkey).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


@router.get("/intents/mine", response_model=OwnedIntentsResponse)
async def get_my_intents(
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> OwnedIntentsResponse:
    intents = await list_reusable_owned_intents(session, agent)
    return OwnedIntentsResponse(
        intents=[_to_response(intent, agent.pubkey) for intent in intents],
    )


@router.get("/intents/{intent_id}", response_model=IntentResponse)
async def get_intent(
    intent_id: str,
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    intent = await get_visible_intent(session, agent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    # The response must carry the *owner's* pubkey, not the caller's.
    # When the caller is fetching their own intent these are identical;
    # when they're fetching a counterparty's intent (visible via an
    # active/pending session), using the caller's pubkey misattributes
    # the intent to the requester.
    if intent.agent_id == agent.id:
        owner_pubkey = agent.pubkey
    else:
        owner_pubkey = await get_agent_pubkey_by_id(session, intent.agent_id) or ""
    return _to_response(intent, owner_pubkey)


@router.delete("/intents/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_intent(
    intent_id: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    intent = await get_owned_intent(session, agent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    await soft_delete_intent(session, intent)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/intents/{intent_id}/refresh", response_model=IntentResponse)
async def refresh_intent(
    intent_id: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    intent = await get_owned_intent(session, agent, intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="intent not found")
    if intent.status in {"expired", "matched", "deleted"}:
        return _error_response(409, "intent_not_refreshable")

    refreshed = await refresh_owned_intent(session, intent, _now())
    return _to_response(refreshed, agent.pubkey)
