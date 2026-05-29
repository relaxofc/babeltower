from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.auth import SIGNED_AGENT_DEPENDENCY
from babeltower.db import get_session
from babeltower.models import Agent, Block, Intent, Session

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)


def _now() -> datetime:
    return datetime.now(UTC)


async def delete_agent_account(session: AsyncSession, agent: Agent, deleted_at: datetime) -> None:
    if hasattr(session, "delete_agent_account"):
        await session.delete_agent_account(agent, deleted_at)  # type: ignore[attr-defined]
        return

    await session.execute(
        update(Intent)
        .where(Intent.agent_id == agent.id, Intent.status != "deleted")
        .values(status="deleted")
    )
    await session.execute(
        delete(Block).where(or_(Block.blocker_id == agent.id, Block.blocked_id == agent.id))
    )
    await session.execute(
        update(Session)
        .where(
            or_(Session.agent_a_id == agent.id, Session.agent_b_id == agent.id),
            Session.status != "closed",
        )
        .values(status="closed", closed_at=deleted_at, close_reason="account_deleted")
    )
    agent.github_user_id = None
    agent.last_seen_at = None
    agent.status = "deleted"
    agent.soft_ban_lifts_at = None
    agent.intents_created_total = 0
    agent.connection_requests_sent_total = 0
    agent.connection_requests_received_total = 0
    agent.connection_requests_accepted_total = 0
    agent.sessions_started_total = 0
    agent.matches_confirmed_total = 0
    agent.blocks_received_total = 0
    await session.commit()


@router.delete("/agent", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    # Account deletion is allowed even while soft-banned. PROTOCOL.md §11
    # guarantees an agent can erase its own data at any time; gating this
    # behind MUTATION_AGENT_DEPENDENCY would trap soft-banned users until
    # the ban lifted.
    agent: Agent = SIGNED_AGENT_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> Response:
    await delete_agent_account(session, agent, _now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
