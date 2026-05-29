from __future__ import annotations

from datetime import UTC, datetime, timedelta

from babeltower.jobs import run_maintenance
from babeltower.models import Agent, ConnectionRequest, Intent, Session


class FakeMaintenanceSession:
    def __init__(self):
        self.agents = []
        self.intents = []
        self.requests = []
        self.sessions = []

    async def run_maintenance(self, now: datetime):
        cutoff = now - timedelta(minutes=5)
        active_agent_ids = {
            agent.id for agent in self.agents if agent.last_seen_at and agent.last_seen_at >= cutoff
        }
        inactive_agent_ids = {agent.id for agent in self.agents if agent.id not in active_agent_ids}

        for intent in self.intents:
            if intent.status == "active" and intent.agent_id in inactive_agent_ids:
                intent.status = "dormant"
            if (
                intent.status == "dormant"
                and intent.agent_id in active_agent_ids
                and intent.expires_at > now
            ):
                intent.status = "active"
            if intent.status in {"active", "dormant"} and intent.expires_at <= now:
                intent.status = "expired"

        for request in self.requests:
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
                request.responded_at = now

        for session in self.sessions:
            if session.status == "awaiting_join" and session.expires_at <= now:
                session.status = "closed"
                session.closed_at = now
                session.close_reason = "awaiting_join_expired"
            elif (
                session.status in {"active", "match_proposed"}
                and session.expires_at <= now
            ):
                session.status = "closed"
                session.closed_at = now
                session.close_reason = "time_limit_reached"
            elif session.status == "match_confirmed" and session.expires_at <= now:
                session.status = "closed"
                session.closed_at = now
                session.close_reason = "handoff_complete"

        for agent in self.agents:
            if agent.status == "soft_banned" and agent.soft_ban_lifts_at <= now:
                agent.status = "active"
                agent.soft_ban_lifts_at = None


class FakeSessionFactory:
    def __init__(self, session: FakeMaintenanceSession):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


def _agent(agent_id: str, last_seen_at: datetime | None):
    return Agent(
        id=agent_id,
        pubkey=f"pubkey_{agent_id}",
        github_user_id=1,
        status="active",
        last_seen_at=last_seen_at,
    )


def _intent(agent_id: str, now: datetime, *, status: str, expires_delta: timedelta):
    return Intent(
        id=f"int_{agent_id}_{status}",
        agent_id=agent_id,
        match_type="co-founder-technical",
        seeking="technical co-founder",
        offering="biotech founder",
        constraints="",
        filters={},
        embedding=[0.01] * 1024,
        ttl_days=30,
        created_at=now - timedelta(days=1),
        expires_at=now + expires_delta,
        status=status,
    )


async def test_maintenance_flips_dormant_active_and_expires_records():
    now = datetime.now(UTC)
    session = FakeMaintenanceSession()
    inactive = _agent("agt_inactive", now - timedelta(minutes=6))
    active = _agent("agt_active", now)
    session.agents = [inactive, active]
    inactive_intent = _intent(inactive.id, now, status="active", expires_delta=timedelta(days=1))
    active_intent = _intent(active.id, now, status="dormant", expires_delta=timedelta(days=1))
    expired_intent = _intent(active.id, now, status="active", expires_delta=timedelta(seconds=-1))
    expired_request = ConnectionRequest(
        id="req_expired",
        from_agent_id=inactive.id,
        to_agent_id=active.id,
        target_intent_id=active_intent.id,
        from_intent_id=inactive_intent.id,
        status="pending",
        created_at=now - timedelta(days=4),
        expires_at=now - timedelta(seconds=1),
    )
    expired_session = Session(
        id="ses_expired",
        agent_a_id=inactive.id,
        agent_b_id=active.id,
        connection_request_id=expired_request.id,
        status="awaiting_join",
        created_at=now - timedelta(days=4),
        expires_at=now - timedelta(seconds=1),
        message_count=0,
    )
    session.intents = [inactive_intent, active_intent, expired_intent]
    session.requests = [expired_request]
    session.sessions = [expired_session]

    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now)

    assert inactive_intent.status == "dormant"
    assert active_intent.status == "active"
    assert expired_intent.status == "expired"
    assert expired_request.status == "expired"
    assert expired_session.status == "closed"
    assert expired_session.close_reason == "awaiting_join_expired"


async def test_maintenance_reactivates_after_poll():
    now = datetime.now(UTC)
    session = FakeMaintenanceSession()
    agent = _agent("agt_returned", now - timedelta(minutes=6))
    intent = _intent(agent.id, now, status="active", expires_delta=timedelta(days=1))
    session.agents = [agent]
    session.intents = [intent]

    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now)
    agent.last_seen_at = now + timedelta(seconds=1)
    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now + timedelta(seconds=1))

    assert intent.status == "active"


async def test_maintenance_lifts_expired_soft_ban():
    now = datetime.now(UTC)
    agent = _agent("agt_soft_banned", now)
    agent.status = "soft_banned"
    agent.soft_ban_lifts_at = now - timedelta(seconds=1)
    session = FakeMaintenanceSession()
    session.agents = [agent]

    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now)

    assert agent.status == "active"
    assert agent.soft_ban_lifts_at is None


def _session(session_id: str, *, status: str, expires_at: datetime):
    return Session(
        id=session_id,
        agent_a_id="agt_a",
        agent_b_id="agt_b",
        connection_request_id=f"req_{session_id}",
        status=status,
        created_at=expires_at - timedelta(minutes=30),
        expires_at=expires_at,
        message_count=0,
    )


async def test_maintenance_closes_active_session_past_wall_clock():
    """Regression: the 30-min active-session deadline used to live only in
    an asyncio task in SessionManager. After a restart, that task was lost
    and the session row in the DB kept its 72h awaiting_join expiry, so
    nothing ever closed sessions that had aged past the protocol limit.
    Now `expires_at` carries the durable deadline for whichever state the
    session is in, and the maintenance job closes anything past it."""
    now = datetime.now(UTC)
    session = FakeMaintenanceSession()
    past = now - timedelta(seconds=1)
    future = now + timedelta(minutes=10)
    session.sessions = [
        _session("ses_active_expired", status="active", expires_at=past),
        _session("ses_active_ok", status="active", expires_at=future),
        _session("ses_handoff_expired", status="match_confirmed", expires_at=past),
        _session("ses_proposed_expired", status="match_proposed", expires_at=past),
    ]

    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now)

    by_id = {s.id: s for s in session.sessions}
    assert by_id["ses_active_expired"].status == "closed"
    assert by_id["ses_active_expired"].close_reason == "time_limit_reached"
    assert by_id["ses_proposed_expired"].close_reason == "time_limit_reached"
    assert by_id["ses_handoff_expired"].status == "closed"
    assert by_id["ses_handoff_expired"].close_reason == "handoff_complete"
    # Sessions whose deadline is still in the future stay open.
    assert by_id["ses_active_ok"].status == "active"
