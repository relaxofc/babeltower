from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import MUTATION_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.metrics import matches_confirmed_total
from babeltower.models import Agent, Session
from babeltower.relay import session_manager
from babeltower.schemas import (
    MatchAcceptResponse,
    MatchProposeResponse,
    MatchRejectRequest,
    MatchRejectResponse,
    MatchSessionRequest,
)

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_response(status_code: int, error_code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error_code": error_code})


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


async def get_agent_pubkey(session: AsyncSession, agent_id: str) -> str:
    if hasattr(session, "get_agent_pubkey_by_id"):
        return await session.get_agent_pubkey_by_id(agent_id)  # type: ignore[attr-defined]

    result = await session.execute(select(Agent.pubkey).where(Agent.id == agent_id))
    pubkey = result.scalar_one()
    return str(pubkey)


async def get_agent_by_id(session: AsyncSession, agent_id: str) -> Optional[Agent]:
    if hasattr(session, "get_agent_by_id"):
        return await session.get_agent_by_id(agent_id)  # type: ignore[attr-defined]
    if not hasattr(session, "get"):
        return None
    return await session.get(Agent, agent_id)


@router.post("/match/propose", response_model=MatchProposeResponse)
async def propose_match(
    body: MatchSessionRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    session_row = await get_member_session(session, body.session_id, agent)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session_row.status == "match_proposed" and session_row.match_proposed_by_id == agent.id:
        return MatchProposeResponse(
            session_id=session_row.id,
            match_status="proposed",
            proposed_by=agent.pubkey,
        )
    if session_row.status != "active":
        return _error_response(409, "session_not_active")

    proposed_at = _now()
    session_row.status = "match_proposed"
    session_row.match_proposed_by_id = agent.id
    session_row.match_proposed_at = proposed_at
    await session.commit()
    await session_manager.emit_to_counterparty(
        session_row.id,
        agent.id,
        "match_proposed",
        {"proposed_by": agent.pubkey, "proposed_at": proposed_at.isoformat()},
    )
    return MatchProposeResponse(
        session_id=session_row.id,
        match_status="proposed",
        proposed_by=agent.pubkey,
    )


@router.post("/match/accept", response_model=MatchAcceptResponse)
async def accept_match(
    body: MatchSessionRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    session_row = await get_member_session(session, body.session_id, agent)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session_row.status != "match_proposed" or session_row.match_proposed_by_id is None:
        return _error_response(409, "match_not_proposed")
    if session_row.match_proposed_by_id == agent.id:
        return _error_response(403, "proposer_cannot_accept")

    confirmed_at = _now()
    proposer_pubkey = await get_agent_pubkey(session, session_row.match_proposed_by_id)
    session_row.status = "match_confirmed"
    session_row.match_confirmed_at = confirmed_at
    agent.matches_confirmed_total = (agent.matches_confirmed_total or 0) + 1
    proposer = await get_agent_by_id(session, session_row.match_proposed_by_id)
    if proposer is not None:
        proposer.matches_confirmed_total = (proposer.matches_confirmed_total or 0) + 1
    await session.commit()
    matches_confirmed_total.inc()
    await session_manager.emit_to_session(
        session_row.id,
        "match_confirmed",
        {
            "confirmed_at": confirmed_at.isoformat(),
            "proposed_by": proposer_pubkey,
            "accepted_by": agent.pubkey,
        },
    )
    session_manager.schedule_handoff_close(session_row.id)
    return MatchAcceptResponse(
        session_id=session_row.id,
        match_status="confirmed",
        confirmed_at=confirmed_at,
    )


@router.post("/match/reject", response_model=MatchRejectResponse)
async def reject_match(
    body: MatchRejectRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    session_row = await get_member_session(session, body.session_id, agent)
    if session_row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session_row.status != "match_proposed" or session_row.match_proposed_by_id is None:
        return _error_response(409, "match_not_proposed")
    if session_row.match_proposed_by_id == agent.id:
        return _error_response(403, "proposer_cannot_reject")

    session_row.status = "active"
    session_row.match_proposed_by_id = None
    session_row.match_proposed_at = None
    await session.commit()
    await session_manager.emit_to_session(
        session_row.id,
        "match_rejected",
        {
            "rejected_by": agent.pubkey,
            "reason": body.reason,
        },
    )
    return MatchRejectResponse(session_id=session_row.id, match_status="rejected")
