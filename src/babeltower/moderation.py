from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower.metrics import soft_bans_applied_total
from babeltower.models import AbuseEvent, Agent, Block, ConnectionRequest, Intent

EMAIL_RE = re.compile(r"[\w._%+-]+@(?:[\w.-]+\.[a-zA-Z]{2,}|[A-Za-z][\w-]{2,})")
PHONE_RE = re.compile(
    r"""
    (?:
        \+?\d{1,3}[\s.-]?
        (?:\(?\d{2,4}\)?[\s.-]?)?
        \d{3,4}[\s.-]?\d{4}
    )
    |
    (?:
        \(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}
    )
    """,
    re.VERBOSE,
)
URL_RE = re.compile(
    r"https?://\S+|\b[\w-]+\.(?:com|net|org|io|ai|xyz|dev|co|me|app)\b",
    re.IGNORECASE,
)
HANDLE_RE = re.compile(r"\B@[A-Za-z][A-Za-z0-9_]{1,}|t\.me/\w+", re.IGNORECASE)


def scan_intent_text(text: str) -> list[str]:
    violations = []
    url_scan_text = text

    if EMAIL_RE.search(text):
        violations.append("email")
        url_scan_text = EMAIL_RE.sub(" ", url_scan_text)
    if PHONE_RE.search(text):
        violations.append("phone")
    if HANDLE_RE.search(text):
        violations.append("handle")
        url_scan_text = HANDLE_RE.sub(" ", url_scan_text)
    if URL_RE.search(url_scan_text):
        violations.append("url")

    return violations


def utc_now() -> datetime:
    return datetime.now(UTC)


async def _count_blocks_received_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Block)
            .where(Block.blocked_id == agent_id, Block.created_at >= since)
        )
        or 0
    )


async def _count_connections_sent_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
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


async def _count_connections_sent_accepted_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
    # The spam ratio (PROTOCOL.md §8.2) measures how many of the requests
    # this agent *sent* got accepted, so the denominator must filter on
    # from_agent_id. Filtering on to_agent_id (requests others sent to this
    # agent that it accepted) is unrelated to its outbound acceptance rate
    # and lets a spammer who also accepts incoming requests dodge the ban.
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ConnectionRequest)
            .where(
                ConnectionRequest.from_agent_id == agent_id,
                ConnectionRequest.status == "accepted",
                ConnectionRequest.responded_at >= since,
            )
        )
        or 0
    )


async def _count_intents_created_since(
    session: AsyncSession,
    agent_id: str,
    since: datetime,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Intent)
            .where(Intent.agent_id == agent_id, Intent.created_at >= since)
        )
        or 0
    )


async def apply_soft_ban(
    session: AsyncSession,
    agent: Agent,
    reason: str,
    now: Optional[datetime] = None,
    details: Optional[dict] = None,
) -> bool:
    now = now or utc_now()
    if agent.status == "soft_banned":
        return False

    agent.status = "soft_banned"
    agent.soft_ban_lifts_at = now + timedelta(days=7)
    session.add(
        AbuseEvent(
            agent_id=agent.id,
            event_type="soft_ban_applied",
            details={"reason": reason, **(details or {})},
            created_at=now,
        )
    )
    await session.execute(
        Intent.__table__.update()
        .where(Intent.agent_id == agent.id, Intent.status == "active")
        .values(status="dormant")
    )
    soft_bans_applied_total.inc()
    return True


async def check_and_apply_soft_ban(
    session: AsyncSession,
    agent_id: str,
    now: Optional[datetime] = None,
) -> bool:
    if hasattr(session, "check_and_apply_soft_ban"):
        return await session.check_and_apply_soft_ban(agent_id, now)  # type: ignore[attr-defined]
    if not hasattr(session, "get"):
        return False

    now = now or utc_now()
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.status in {"soft_banned", "hard_banned", "deleted"}:
        return False

    seven_days_ago = now - timedelta(days=7)
    one_day_ago = now - timedelta(days=1)

    blocks_received = await _count_blocks_received_since(session, agent_id, seven_days_ago)
    if blocks_received > 5:
        return await apply_soft_ban(
            session,
            agent,
            "blocks_received",
            now,
            {"blocks_received_7d": blocks_received},
        )

    sent_7d = await _count_connections_sent_since(session, agent_id, seven_days_ago)
    accepted_7d = await _count_connections_sent_accepted_since(session, agent_id, seven_days_ago)
    denominator = max(accepted_7d, 1)
    if sent_7d >= 50 and sent_7d / denominator > 50:
        return await apply_soft_ban(
            session,
            agent,
            "connection_spam",
            now,
            {
                "connection_requests_sent_7d": sent_7d,
                "connection_requests_accepted_7d": accepted_7d,
            },
        )

    intents_24h = await _count_intents_created_since(session, agent_id, one_day_ago)
    if intents_24h > 100:
        return await apply_soft_ban(
            session,
            agent,
            "intent_creation_burst",
            now,
            {"intents_created_24h": intents_24h},
        )

    return False
