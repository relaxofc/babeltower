from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.crypto import canonical_request_string, verify
from babeltower.db import get_session
from babeltower.models import Agent

SIGNATURE_WINDOW_SECONDS = 60
MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SESSION_DEPENDENCY = Depends(get_session)


def _parse_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid timestamp") from exc
    if timestamp.tzinfo is None:
        raise HTTPException(status_code=401, detail="invalid timestamp")
    return timestamp.astimezone(UTC)


def _validate_timestamp(value: str) -> None:
    timestamp = _parse_timestamp(value)
    age = abs((datetime.now(UTC) - timestamp).total_seconds())
    if age > SIGNATURE_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="timestamp outside allowed window")


async def get_agent_by_pubkey(session: AsyncSession, pubkey: str) -> Optional[Agent]:
    if hasattr(session, "get_agent_by_pubkey"):
        return await session.get_agent_by_pubkey(pubkey)  # type: ignore[attr-defined]

    result = await session.execute(select(Agent).where(Agent.pubkey == pubkey))
    return result.scalar_one_or_none()


async def verify_signed_request(
    request: Request,
    x_agent_pubkey: Optional[str] = Header(None, alias="X-Agent-Pubkey"),
    x_timestamp: Optional[str] = Header(None, alias="X-Timestamp"),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Agent:
    if not x_agent_pubkey or not x_timestamp or not x_signature:
        raise HTTPException(status_code=401, detail="missing signature headers")

    body = await request.body()
    canonical = canonical_request_string(
        request.method,
        request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        x_timestamp,
        body,
    )

    _validate_timestamp(x_timestamp)
    if not verify(x_agent_pubkey, canonical, x_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    agent = await get_agent_by_pubkey(session, x_agent_pubkey)
    if agent is None or agent.status in {"hard_banned", "deleted"}:
        raise HTTPException(status_code=401, detail="agent not registered")

    request.state.agent = agent
    return agent


SIGNED_AGENT_DEPENDENCY = Depends(verify_signed_request)


async def require_agent_can_mutate(
    request: Request,
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
) -> Agent:
    is_banned_mutation = (
        request.method.upper() in MUTATION_METHODS
        and agent.status in {"soft_banned", "hard_banned"}
    )
    if is_banned_mutation:
        raise HTTPException(status_code=403, detail="agent cannot mutate while banned")
    return agent
MUTATION_AGENT_DEPENDENCY = Depends(require_agent_can_mutate)
