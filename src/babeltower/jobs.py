from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from babeltower.db import async_session
from babeltower.models import Agent, ConnectionRequest, Intent, Session


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_maintenance(
    session_factory: async_sessionmaker[AsyncSession] = async_session,
    now_func: Callable[[], datetime] = utc_now,
) -> None:
    now = now_func()
    cutoff = now - timedelta(minutes=5)

    async with session_factory() as session:
        if hasattr(session, "run_maintenance"):
            await session.run_maintenance(now)  # type: ignore[attr-defined]
            return

        # An agent that has never polled the inbox (last_seen_at IS NULL) has
        # by definition not polled within the 5-minute window, so its intents
        # must be treated as dormant (PROTOCOL.md §5.2). `last_seen_at < cutoff`
        # alone silently skips NULLs, leaving never-polled agents' intents
        # "active" forever.
        inactive_agents = select(Agent.id).where(
            or_(Agent.last_seen_at < cutoff, Agent.last_seen_at.is_(None))
        )
        active_agents = select(Agent.id).where(Agent.last_seen_at >= cutoff)

        await session.execute(
            update(Agent)
            .where(Agent.status == "soft_banned", Agent.soft_ban_lifts_at <= now)
            .values(status="active", soft_ban_lifts_at=None)
        )

        await session.execute(
            update(Intent)
            .where(Intent.status == "active", Intent.agent_id.in_(inactive_agents))
            .values(status="dormant")
        )
        await session.execute(
            update(Intent)
            .where(
                Intent.status == "dormant",
                Intent.agent_id.in_(active_agents),
                Intent.expires_at > now,
            )
            .values(status="active")
        )
        await session.execute(
            update(ConnectionRequest)
            .where(
                ConnectionRequest.status == "pending",
                ConnectionRequest.expires_at <= now,
            )
            .values(status="expired", responded_at=now)
        )
        await session.execute(
            update(Session)
            .where(Session.status == "awaiting_join", Session.expires_at <= now)
            .values(
                status="closed",
                closed_at=now,
                close_reason="awaiting_join_expired",
            )
        )
        # Active and match_proposed sessions whose 30-min wall clock has
        # elapsed (active_at + 30min) — these used to depend solely on the
        # in-memory monitor_task, so an API restart left them alive forever.
        # expires_at is now the durable deadline, written when the session
        # transitions to active.
        await session.execute(
            update(Session)
            .where(
                Session.status.in_(("active", "match_proposed")),
                Session.expires_at <= now,
            )
            .values(
                status="closed",
                closed_at=now,
                close_reason="time_limit_reached",
            )
        )
        # Match_confirmed sessions whose 10-min handoff window has elapsed.
        await session.execute(
            update(Session)
            .where(
                Session.status == "match_confirmed",
                Session.expires_at <= now,
            )
            .values(
                status="closed",
                closed_at=now,
                close_reason="handoff_complete",
            )
        )
        await session.execute(
            update(Intent)
            .where(Intent.status.in_(("active", "dormant")), Intent.expires_at <= now)
            .values(status="expired")
        )
        await session.commit()


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_maintenance,
        "interval",
        seconds=60,
        id="babeltower_maintenance",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler


async def shutdown_scheduler(scheduler: Any) -> None:
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
