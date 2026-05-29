from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import MUTATION_AGENT_DEPENDENCY
from babeltower.config import get_settings
from babeltower.db import get_session
from babeltower.models import Agent, Block, ConnectionRequest, Intent, Session
from babeltower.moderation import check_and_apply_soft_ban
from babeltower.schemas import (
    ConnectionCreateRequest,
    ConnectionCreateResponse,
    RejectConnectionRequest,
    SessionAcceptResponse,
)

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)
REQUEST_TTL = timedelta(hours=72)


def _now() -> datetime:
    return datetime.now(UTC)


def _error_response(status_code: int, error_code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error_code": error_code})


def _ws_url(session_id: str) -> str:
    base_url = get_settings().server_base_url.rstrip("/")
    if base_url.startswith("https://"):
        ws_base_url = "wss://" + base_url.removeprefix("https://")
    elif base_url.startswith("http://"):
        ws_base_url = "ws://" + base_url.removeprefix("http://")
    else:
        ws_base_url = base_url
    return f"{ws_base_url}/v1/session/{session_id}"


async def load_connection_intents(
    session: AsyncSession,
    agent: Agent,
    body: ConnectionCreateRequest,
) -> tuple[Optional[Intent], Optional[Intent], Optional[Agent]]:
    if hasattr(session, "load_connection_intents"):
        return await session.load_connection_intents(agent, body)  # type: ignore[attr-defined]

    target_result = await session.execute(
        select(Intent, Agent)
        .join(Agent, Agent.id == Intent.agent_id)
        .where(Intent.id == body.target_intent_id)
    )
    target_row = target_result.one_or_none()
    target_intent = target_row[0] if target_row is not None else None
    target_agent = target_row[1] if target_row is not None else None

    from_result = await session.execute(
        select(Intent).where(Intent.id == body.from_intent_id, Intent.agent_id == agent.id)
    )
    from_intent = from_result.scalar_one_or_none()
    return target_intent, from_intent, target_agent


async def has_block_between(session: AsyncSession, a_id: str, b_id: str) -> bool:
    if hasattr(session, "has_block_between"):
        return await session.has_block_between(a_id, b_id)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Block.id).where(
            or_(
                (Block.blocker_id == a_id) & (Block.blocked_id == b_id),
                (Block.blocker_id == b_id) & (Block.blocked_id == a_id),
            )
        )
    )
    return result.scalar_one_or_none() is not None


async def count_pending_outbound(session: AsyncSession, agent_id: str, now: datetime) -> int:
    if hasattr(session, "count_pending_outbound"):
        return await session.count_pending_outbound(agent_id, now)  # type: ignore[attr-defined]

    return int(
        await session.scalar(
            select(func.count())
            .select_from(ConnectionRequest)
            .where(
                ConnectionRequest.from_agent_id == agent_id,
                ConnectionRequest.status == "pending",
                ConnectionRequest.expires_at > now,
            )
        )
        or 0
    )


async def count_requests_sent_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
    if hasattr(session, "count_requests_sent_since"):
        return await session.count_requests_sent_since(agent_id, since)  # type: ignore[attr-defined]

    return int(
        await session.scalar(
            select(func.count())
            .select_from(ConnectionRequest)
            .where(
                ConnectionRequest.from_agent_id == agent_id,
                ConnectionRequest.created_at >= since,
            )
        )
        or 0
    )


async def create_connection_request(
    session: AsyncSession,
    agent: Agent,
    target_agent: Agent,
    body: ConnectionCreateRequest,
    created_at: datetime,
) -> ConnectionRequest:
    if hasattr(session, "create_connection_request"):
        return await session.create_connection_request(  # type: ignore[attr-defined]
            agent,
            target_agent,
            body,
            created_at,
        )

    request = ConnectionRequest(
        from_agent_id=agent.id,
        to_agent_id=target_agent.id,
        target_intent_id=body.target_intent_id,
        from_intent_id=body.from_intent_id,
        opening_message=body.opening_message,
        status="pending",
        created_at=created_at,
        expires_at=created_at + REQUEST_TTL,
    )
    agent.connection_requests_sent_total = (agent.connection_requests_sent_total or 0) + 1
    target_agent.connection_requests_received_total = (
        target_agent.connection_requests_received_total or 0
    ) + 1
    session.add(request)
    await session.commit()
    await session.refresh(request)
    return request


async def get_request_for_action(
    session: AsyncSession,
    request_id: str,
) -> Optional[ConnectionRequest]:
    if hasattr(session, "get_connection_request"):
        return await session.get_connection_request(request_id)  # type: ignore[attr-defined]
    return await session.get(ConnectionRequest, request_id)


async def accept_connection_request(
    session: AsyncSession,
    request: ConnectionRequest,
    target_agent: Agent,
    accepted_at: datetime,
) -> Session:
    if hasattr(session, "accept_connection_request"):
        return await session.accept_connection_request(  # type: ignore[attr-defined]
            request,
            target_agent,
            accepted_at,
        )

    request.status = "accepted"
    request.responded_at = accepted_at
    session_row = Session(
        agent_a_id=request.from_agent_id,
        agent_b_id=request.to_agent_id,
        connection_request_id=request.id,
        status="awaiting_join",
        created_at=accepted_at,
        expires_at=accepted_at + REQUEST_TTL,
        message_count=0,
    )
    target_agent.connection_requests_accepted_total = (
        target_agent.connection_requests_accepted_total or 0
    ) + 1
    session.add(session_row)
    await session.commit()
    await session.refresh(session_row)
    return session_row


async def update_request_status(
    session: AsyncSession,
    request: ConnectionRequest,
    status_value: str,
    responded_at: datetime,
    reason: Optional[str] = None,
) -> None:
    if hasattr(session, "update_connection_request_status"):
        await session.update_connection_request_status(  # type: ignore[attr-defined]
            request,
            status_value,
            responded_at,
            reason,
        )
        return

    request.status = status_value
    request.responded_at = responded_at
    request.rejection_reason = reason if status_value == "rejected" else request.rejection_reason
    await session.commit()


@router.post(
    "/connect",
    response_model=ConnectionCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    body: ConnectionCreateRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    created_at = _now()
    target_intent, from_intent, target_agent = await load_connection_intents(session, agent, body)
    if target_intent is None or target_agent is None or target_intent.status != "active":
        raise HTTPException(status_code=404, detail="target intent not found")
    if from_intent is None or from_intent.status != "active":
        raise HTTPException(status_code=404, detail="from intent not found")
    if target_agent.id == agent.id:
        return _error_response(400, "cannot_connect_to_self")
    if await has_block_between(session, agent.id, target_agent.id):
        return _error_response(403, "blocked")

    if await count_pending_outbound(session, agent.id, created_at) >= 20:
        return _error_response(409, "connection_request_limit_reached")
    if await count_requests_sent_since(session, agent.id, created_at - timedelta(days=1)) >= 50:
        return _error_response(429, "rate_limited")

    request = await create_connection_request(session, agent, target_agent, body, created_at)
    await check_and_apply_soft_ban(session, agent.id, created_at)
    if hasattr(session, "commit"):
        await session.commit()
    return ConnectionCreateResponse(
        request_id=request.id,
        target_agent_pubkey=target_agent.pubkey,
        status=request.status,
        expires_at=request.expires_at,
    )


@router.post(
    "/connect/{request_id}/accept",
    response_model=SessionAcceptResponse,
    status_code=status.HTTP_201_CREATED,
)
async def accept_connection(
    request_id: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
):
    connection_request = await get_request_for_action(session, request_id)
    now = _now()
    if connection_request is None or connection_request.to_agent_id != agent.id:
        raise HTTPException(status_code=404, detail="connection request not found")
    if connection_request.status != "pending" or connection_request.expires_at <= now:
        return _error_response(409, "connection_request_not_pending")

    session_row = await accept_connection_request(session, connection_request, agent, now)
    return SessionAcceptResponse(
        session_id=session_row.id,
        ws_url=_ws_url(session_row.id),
        expires_at=session_row.expires_at,
    )


@router.post("/connect/{request_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_connection(
    request_id: str,
    body: Optional[RejectConnectionRequest] = None,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    connection_request = await get_request_for_action(session, request_id)
    now = _now()
    if connection_request is None or connection_request.to_agent_id != agent.id:
        raise HTTPException(status_code=404, detail="connection request not found")
    if connection_request.status != "pending":
        return _error_response(409, "connection_request_not_pending")
    await update_request_status(
        session,
        connection_request,
        "rejected",
        now,
        body.reason if body is not None else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connect/{request_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_connection(
    request_id: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    connection_request = await get_request_for_action(session, request_id)
    if connection_request is None or connection_request.from_agent_id != agent.id:
        raise HTTPException(status_code=404, detail="connection request not found")
    if connection_request.status != "pending":
        return _error_response(409, "connection_request_not_pending")
    await update_request_status(session, connection_request, "cancelled", _now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
