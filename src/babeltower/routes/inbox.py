from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from babeltower.auth import SIGNED_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.models import Agent, ConnectionRequest, Intent, Session
from babeltower.rate_limit import limiter
from babeltower.schemas import (
    InboxMatchedHandoff,
    InboxMatchProposal,
    InboxPendingRequest,
    InboxRejectedRequest,
    InboxResponse,
    InboxSession,
    IntentResponse,
)

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)


def _now() -> datetime:
    return datetime.now(UTC)


def _intent_response(intent: Intent, agent_pubkey: str) -> IntentResponse:
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


async def load_inbox(session: AsyncSession, agent: Agent, now: datetime) -> InboxResponse:
    if hasattr(session, "load_inbox"):
        return await session.load_inbox(agent, now)  # type: ignore[attr-defined]

    agent.last_seen_at = now

    from_agent = aliased(Agent)
    from_intent = aliased(Intent)
    pending_result = await session.execute(
        select(ConnectionRequest, from_agent, from_intent)
        .join(from_agent, from_agent.id == ConnectionRequest.from_agent_id)
        .join(from_intent, from_intent.id == ConnectionRequest.from_intent_id)
        .where(
            ConnectionRequest.to_agent_id == agent.id,
            ConnectionRequest.status == "pending",
            ConnectionRequest.expires_at > now,
        )
        .order_by(ConnectionRequest.created_at)
    )
    pending_requests = [
        InboxPendingRequest(
            request_id=request_row.id,
            from_agent_pubkey=sender.pubkey,
            from_intent=_intent_response(intent, sender.pubkey),
            target_intent_id=request_row.target_intent_id,
            opening_message=request_row.opening_message,
            received_at=request_row.created_at,
            expires_at=request_row.expires_at,
        )
        for request_row, sender, intent in pending_result
    ]

    agent_a = aliased(Agent)
    agent_b = aliased(Agent)
    awaiting_result = await session.execute(
        select(
            Session,
            agent_a.pubkey.label("agent_a_pubkey"),
            agent_b.pubkey.label("agent_b_pubkey"),
            ConnectionRequest.from_intent_id,
            ConnectionRequest.target_intent_id,
        )
        .join(agent_a, agent_a.id == Session.agent_a_id)
        .join(agent_b, agent_b.id == Session.agent_b_id)
        .join(ConnectionRequest, ConnectionRequest.id == Session.connection_request_id)
        .where(
            or_(Session.agent_a_id == agent.id, Session.agent_b_id == agent.id),
            Session.status == "awaiting_join",
            Session.expires_at > now,
        )
        .order_by(Session.created_at)
    )
    accepted_sessions = []
    for session_row, agent_a_pubkey, agent_b_pubkey, from_intent_id, target_intent_id in (
        awaiting_result
    ):
        # agent_a is always the requester (from_intent), agent_b the target
        # (target_intent); see accept_connection_request in routes/connections.
        is_requester = session_row.agent_a_id == agent.id
        accepted_sessions.append(
            InboxSession(
                session_id=session_row.id,
                counterparty_pubkey=(agent_b_pubkey if is_requester else agent_a_pubkey),
                accepted_at=session_row.created_at,
                session_expires_at=session_row.expires_at,
                my_intent_id=from_intent_id if is_requester else target_intent_id,
                counterparty_intent_id=target_intent_id if is_requester else from_intent_id,
            )
        )

    proposal_result = await session.execute(
        select(Session, Agent.pubkey)
        .join(Agent, Agent.id == Session.match_proposed_by_id)
        .where(
            or_(Session.agent_a_id == agent.id, Session.agent_b_id == agent.id),
            Session.status == "match_proposed",
            Session.match_proposed_by_id != agent.id,
        )
        .order_by(Session.match_proposed_at)
    )
    match_proposals = [
        InboxMatchProposal(
            session_id=session_row.id,
            proposed_by=proposer_pubkey,
            proposed_at=session_row.match_proposed_at or session_row.created_at,
        )
        for session_row, proposer_pubkey in proposal_result
    ]

    handoff_result = await session.execute(
        select(
            Session,
            agent_a.pubkey.label("agent_a_pubkey"),
            agent_b.pubkey.label("agent_b_pubkey"),
        )
        .join(agent_a, agent_a.id == Session.agent_a_id)
        .join(agent_b, agent_b.id == Session.agent_b_id)
        .where(
            or_(Session.agent_a_id == agent.id, Session.agent_b_id == agent.id),
            Session.status == "match_confirmed",
            Session.match_confirmed_at >= now - timedelta(days=1),
        )
        .order_by(Session.match_confirmed_at)
    )
    matched_handoffs = [
        InboxMatchedHandoff(
            session_id=session_row.id,
            counterparty_pubkey=(
                agent_b_pubkey if session_row.agent_a_id == agent.id else agent_a_pubkey
            ),
            matched_at=session_row.match_confirmed_at or session_row.created_at,
        )
        for session_row, agent_a_pubkey, agent_b_pubkey in handoff_result
    ]

    rejected_result = await session.execute(
        select(ConnectionRequest)
        .where(
            ConnectionRequest.from_agent_id == agent.id,
            ConnectionRequest.status == "rejected",
            ConnectionRequest.responded_at >= now - timedelta(days=1),
        )
        .order_by(ConnectionRequest.responded_at.desc())
    )
    recently_rejected = [
        InboxRejectedRequest(
            request_id=request_row.id,
            rejected_at=request_row.responded_at or request_row.created_at,
            reason=request_row.rejection_reason,
        )
        for request_row in rejected_result.scalars()
    ]

    await session.commit()
    return InboxResponse(
        pending_requests=pending_requests,
        accepted_sessions_awaiting_join=accepted_sessions,
        match_proposals=match_proposals,
        matched_handoffs=matched_handoffs,
        recently_rejected=recently_rejected,
    )


@router.get("/inbox", response_model=InboxResponse)
@limiter.limit("120/minute")
async def get_inbox(
    request: Request,
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> InboxResponse:
    del request
    return await load_inbox(session, agent, _now())
