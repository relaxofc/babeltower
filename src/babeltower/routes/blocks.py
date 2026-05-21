from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import MUTATION_AGENT_DEPENDENCY, SIGNED_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.models import Agent, Block, Session
from babeltower.moderation import check_and_apply_soft_ban
from babeltower.relay import session_manager
from babeltower.schemas import BlockRequest, BlockResponse, BlocksResponse

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_agent_by_pubkey(session: AsyncSession, pubkey: str) -> Optional[Agent]:
    if hasattr(session, "get_agent_by_pubkey"):
        return await session.get_agent_by_pubkey(pubkey)  # type: ignore[attr-defined]

    result = await session.execute(select(Agent).where(Agent.pubkey == pubkey))
    return result.scalar_one_or_none()


async def upsert_block(
    session: AsyncSession,
    blocker: Agent,
    blocked: Agent,
    reason: Optional[str],
    now: datetime,
) -> tuple[Block, bool]:
    if hasattr(session, "upsert_block"):
        return await session.upsert_block(blocker, blocked, reason, now)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Block).where(Block.blocker_id == blocker.id, Block.blocked_id == blocked.id)
    )
    block = result.scalar_one_or_none()
    created = block is None
    if block is None:
        block = Block(
            blocker_id=blocker.id,
            blocked_id=blocked.id,
            reason=reason,
            created_at=now,
        )
        blocked.blocks_received_total = (blocked.blocks_received_total or 0) + 1
        session.add(block)
    else:
        block.reason = reason
    return block, created


async def close_sessions_between(
    session: AsyncSession,
    a_id: str,
    b_id: str,
    now: datetime,
) -> list[str]:
    if hasattr(session, "close_sessions_between"):
        return await session.close_sessions_between(a_id, b_id, now)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Session).where(
            or_(
                (Session.agent_a_id == a_id) & (Session.agent_b_id == b_id),
                (Session.agent_a_id == b_id) & (Session.agent_b_id == a_id),
            ),
            Session.status != "closed",
        )
    )
    session_ids = []
    for session_row in result.scalars():
        session_row.status = "closed"
        session_row.closed_at = now
        session_row.close_reason = "blocked"
        session_ids.append(session_row.id)
    return session_ids


async def delete_block_row(session: AsyncSession, blocker: Agent, target: Agent) -> None:
    if hasattr(session, "delete_block"):
        await session.delete_block(blocker, target)  # type: ignore[attr-defined]
        return

    result = await session.execute(
        select(Block).where(Block.blocker_id == blocker.id, Block.blocked_id == target.id)
    )
    block = result.scalar_one_or_none()
    if block is not None:
        await session.delete(block)
    await session.commit()


async def list_block_rows(session: AsyncSession, blocker: Agent) -> list[BlockResponse]:
    if hasattr(session, "list_blocks"):
        return await session.list_blocks(blocker)  # type: ignore[attr-defined]

    result = await session.execute(
        select(Block, Agent)
        .join(Agent, Agent.id == Block.blocked_id)
        .where(Block.blocker_id == blocker.id)
        .order_by(Block.created_at.desc())
    )
    return [
        BlockResponse(
            target_agent_pubkey=target.pubkey,
            reason=block.reason,
            created_at=block.created_at,
        )
        for block, target in result
    ]


@router.post("/block", status_code=status.HTTP_204_NO_CONTENT)
async def block_agent(
    body: BlockRequest,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    target = await get_agent_by_pubkey(session, body.target_agent_pubkey)
    if target is None:
        raise HTTPException(status_code=404, detail="target agent not found")
    if target.id == agent.id:
        raise HTTPException(status_code=400, detail="cannot block self")

    now = _now()
    block, created = await upsert_block(session, agent, target, body.reason, now)
    closed_session_ids = await close_sessions_between(session, agent.id, target.id, now)
    if created:
        await check_and_apply_soft_ban(session, target.id, now)
    await session.commit()
    await session.refresh(block)

    for session_id in closed_session_ids:
        if session_manager.has_session(session_id):
            await session_manager.end_session(session_id, "blocked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/block/{target_agent_pubkey:path}", status_code=status.HTTP_204_NO_CONTENT)
async def unblock_agent(
    target_agent_pubkey: str,
    agent: Agent = MUTATION_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    target = await get_agent_by_pubkey(session, target_agent_pubkey)
    if target is not None:
        await delete_block_row(session, agent, target)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/blocks", response_model=BlocksResponse)
async def get_blocks(
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> BlocksResponse:
    return BlocksResponse(blocks=await list_block_rows(session, agent))
