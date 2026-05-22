from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_base32(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        value, remainder = divmod(value, 32)
        chars.append(_CROCKFORD[remainder])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    timestamp = _encode_base32(int(time.time() * 1000), 10)
    randomness = "".join(secrets.choice(_CROCKFORD) for _ in range(16))
    return f"{prefix}_{timestamp}{randomness}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("agt"))
    pubkey: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    github_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    soft_ban_lifts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    intents_created_total: Mapped[int] = mapped_column(Integer, default=0)
    connection_requests_sent_total: Mapped[int] = mapped_column(Integer, default=0)
    connection_requests_received_total: Mapped[int] = mapped_column(Integer, default=0)
    connection_requests_accepted_total: Mapped[int] = mapped_column(Integer, default=0)
    sessions_started_total: Mapped[int] = mapped_column(Integer, default=0)
    matches_confirmed_total: Mapped[int] = mapped_column(Integer, default=0)
    blocks_received_total: Mapped[int] = mapped_column(Integer, default=0)

    intents: Mapped[list[Intent]] = relationship(back_populates="agent")


class Intent(Base):
    __tablename__ = "intents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("int"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    match_type: Mapped[str] = mapped_column(String(64), index=True)
    seeking: Mapped[str] = mapped_column(Text)
    offering: Mapped[str] = mapped_column(Text)
    constraints: Mapped[str] = mapped_column(String(500), default="")
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    embedding: Mapped[list[float]] = mapped_column(HALFVEC(1024))
    ttl_days: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)

    agent: Mapped[Agent] = relationship(back_populates="intents")


class ConnectionRequest(Base):
    __tablename__ = "connection_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("req"))
    from_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    to_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    target_intent_id: Mapped[str] = mapped_column(ForeignKey("intents.id"), index=True)
    from_intent_id: Mapped[str] = mapped_column(ForeignKey("intents.id"), index=True)
    opening_message: Mapped[Optional[str]] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[Optional[str]] = mapped_column(String(200))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ses"))
    agent_a_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    agent_b_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    connection_request_id: Mapped[str] = mapped_column(
        ForeignKey("connection_requests.id"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="awaiting_join", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
    # expires_at carries the *next* state-deadline:
    #   awaiting_join → original 72h TTL
    #   active        → active_at + 30min (PROTOCOL.md §7.4 wall clock)
    #   match_confirmed → confirmed_at + 10min (handoff window)
    # The maintenance job uses this to durably close sessions whose
    # in-memory deadline timer was lost across an API restart.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[Optional[str]] = mapped_column(String(64))
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    # active_at is set the first time both members join the websocket; the
    # 30-minute wall-clock starts from this instant. Persisted so reconnect
    # after a restart can compute the remaining time instead of restarting
    # the budget from zero.
    active_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    match_proposed_by_id: Mapped[Optional[str]] = mapped_column(ForeignKey("agents.id"))
    match_proposed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    match_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_blocks_pair"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("blk"))
    blocker_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    blocked_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AbuseEvent(Base):
    __tablename__ = "abuse_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("abuse"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )
