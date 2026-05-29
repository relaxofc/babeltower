from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import MUTATION_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.models import Agent, Session
from babeltower.relay import session_manager

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)


def _now() -> datetime:
    return datetime.now(UTC)


async def get_member_session(
    session: AsyncSession,
    session_id: str,
    agent: Agent,
) -> Optional[Session]:
    if hasattr(session, "get_member_session"):
        return await session.get_member_session(session_id, agent)  # type: ignore[attr-defined]

    session_row = await session.get(Session, session_id)
    if session_row is None:
        return None
    if agent.id not in {session_row.agent_a_id, session_row.agent_b_id}:
        return None
    return session_row


async def close_session(
    session: AsyncSession,
    session_row: Session,
    reason: str,
) -> None:
    if hasattr(session, "close_session"):
        await session.close_session(session_row, reason, _now())  # type: ignore[attr-defined]
        return

    session_row.status = "closed"
    session_row.closed_at = _now()
    session_row.close_reason = reason
    await session.commit()


@router.post("/session/{session_id}/end", status_code=status.HTTP_204_NO_CONTENT)
async def end_session(
    session_id: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    session_row = await get_member_session(session, session_id, agent)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session_row.status != "closed":
        await close_session(session, session_row, "ended_by_agent")
        await session_manager.end_session(session_id, "ended_by_agent")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
