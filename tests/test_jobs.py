from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
    session = FakeMaintenanceSession()
    agent = _agent("agt_returned", now - timedelta(minutes=6))
    intent = _intent(agent.id, now, status="active", expires_delta=timedelta(days=1))
    session.agents = [agent]
    session.intents = [intent]

    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now)
    agent.last_seen_at = now + timedelta(seconds=1)
    await run_maintenance(FakeSessionFactory(session), now_func=lambda: now + timedelta(seconds=1))

    assert intent.status == "active"
