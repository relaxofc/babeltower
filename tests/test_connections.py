from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Optional

from babeltower.db import get_session
from babeltower.main import create_app
from babeltower.models import Agent, ConnectionRequest, Intent, Session, new_id
from babeltower.schemas import (
    ConnectionCreateRequest,
    InboxPendingRequest,
    InboxRejectedRequest,
    InboxResponse,
    InboxSession,
    IntentResponse,
)


class FakeConnectionSession:
    def __init__(self, agents):
        self._agents = {agent.public_key: agent.row for agent in agents}
        self.agents_by_id = {agent.row.id: agent.row for agent in agents}
        self.intents = {}
        self.requests = {}
        self.sessions = {}
        self.blocked_pairs = set()
        self.pending_count = 0
        self.daily_count = 0

    async def get_agent_by_pubkey(self, pubkey: str):
        return self._agents.get(pubkey)

    def add_intent(self, agent, *, intent_id: Optional[str] = None, status: str = "active"):
        now = datetime.now(UTC)
        intent = Intent(
            id=intent_id or new_id("int"),
            agent_id=agent.row.id,
            match_type="co-founder-technical",
            seeking="technical co-founder for biotech startup",
            offering="ML engineer with biotech background",
            constraints="Seoul or remote",
            filters={"location": "Seoul"},
            embedding=[0.01] * 1024,
            ttl_days=30,
            created_at=now,
            expires_at=now + timedelta(days=30),
            status=status,
        )
        self.intents[intent.id] = intent
        return intent

    async def load_connection_intents(self, agent: Agent, body: ConnectionCreateRequest):
        target_intent = self.intents.get(body.target_intent_id)
        from_intent = self.intents.get(body.from_intent_id)
        if from_intent is not None and from_intent.agent_id != agent.id:
            from_intent = None
        target_agent = (
            self.agents_by_id.get(target_intent.agent_id) if target_intent is not None else None
        )
        return target_intent, from_intent, target_agent

    async def has_block_between(self, a_id: str, b_id: str):
        return (a_id, b_id) in self.blocked_pairs or (b_id, a_id) in self.blocked_pairs

    async def count_pending_outbound(self, agent_id: str, now: datetime):
        del agent_id, now
        return self.pending_count

    async def count_requests_sent_since(self, agent_id: str, since: datetime):
        del agent_id, since
        return self.daily_count

    async def create_connection_request(
        self,
        agent: Agent,
        target_agent: Agent,
        body: ConnectionCreateRequest,
        created_at: datetime,
    ):
        request = ConnectionRequest(
            id=new_id("req"),
            from_agent_id=agent.id,
            to_agent_id=target_agent.id,
            target_intent_id=body.target_intent_id,
            from_intent_id=body.from_intent_id,
            opening_message=body.opening_message,
            status="pending",
            created_at=created_at,
            expires_at=created_at + timedelta(hours=72),
        )
        self.requests[request.id] = request
        self.pending_count += 1
        self.daily_count += 1
        agent.connection_requests_sent_total = (agent.connection_requests_sent_total or 0) + 1
        target_agent.connection_requests_received_total = (
            target_agent.connection_requests_received_total or 0
        ) + 1
        return request

    async def get_connection_request(self, request_id: str):
        return self.requests.get(request_id)

    async def accept_connection_request(
        self,
        request: ConnectionRequest,
        target_agent: Agent,
        accepted_at: datetime,
    ):
        request.status = "accepted"
        request.responded_at = accepted_at
        session = Session(
            id=new_id("ses"),
            agent_a_id=request.from_agent_id,
            agent_b_id=request.to_agent_id,
            connection_request_id=request.id,
            status="awaiting_join",
            created_at=accepted_at,
            expires_at=accepted_at + timedelta(hours=72),
            message_count=0,
        )
        self.sessions[session.id] = session
        target_agent.connection_requests_accepted_total = (
            target_agent.connection_requests_accepted_total or 0
        ) + 1
        return session

    async def update_connection_request_status(
        self,
        request: ConnectionRequest,
        status_value: str,
        responded_at: datetime,
        reason: Optional[str],
    ):
        request.status = status_value
        request.responded_at = responded_at
        if status_value == "rejected":
            request.rejection_reason = reason

    def _intent_response(self, intent: Intent, pubkey: str):
        return IntentResponse(
            intent_id=intent.id,
            agent_pubkey=pubkey,
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

    async def load_inbox(self, agent: Agent, now: datetime):
        agent.last_seen_at = now
        pending = []
        accepted = []
        rejected = []

        for request in self.requests.values():
            sender = self.agents_by_id[request.from_agent_id]
            if (
                request.to_agent_id == agent.id
                and request.status == "pending"
                and request.expires_at > now
            ):
                pending.append(
                    InboxPendingRequest(
                        request_id=request.id,
                        from_agent_pubkey=sender.pubkey,
                        from_intent=self._intent_response(
                            self.intents[request.from_intent_id],
                            sender.pubkey,
                        ),
                        target_intent_id=request.target_intent_id,
                        opening_message=request.opening_message,
                        received_at=request.created_at,
                        expires_at=request.expires_at,
                    )
                )
            if (
                request.from_agent_id == agent.id
                and request.status == "rejected"
                and request.responded_at is not None
                and request.responded_at >= now - timedelta(days=1)
            ):
                rejected.append(
                    InboxRejectedRequest(
                        request_id=request.id,
                        rejected_at=request.responded_at,
                        reason=request.rejection_reason,
                    )
                )

        for session in self.sessions.values():
            if (
                agent.id in {session.agent_a_id, session.agent_b_id}
                and session.status == "awaiting_join"
                and session.expires_at > now
            ):
                counterparty_id = (
                    session.agent_b_id if session.agent_a_id == agent.id else session.agent_a_id
                )
                accepted.append(
                    InboxSession(
                        session_id=session.id,
                        counterparty_pubkey=self.agents_by_id[counterparty_id].pubkey,
                        accepted_at=session.created_at,
                        session_expires_at=session.expires_at,
                    )
                )

        return InboxResponse(
            pending_requests=pending,
            accepted_sessions_awaiting_join=accepted,
            match_proposals=[],
            matched_handoffs=[],
            recently_rejected=rejected,
        )


def _app_with_connection_overrides(session):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def _connect_payload(target_intent: Intent, from_intent: Intent, **overrides):
    payload = {
        "target_intent_id": target_intent.id,
        "from_intent_id": from_intent.id,
        "opening_message": "This looks like a strong founder fit.",
    }
    payload.update(overrides)
    return payload


async def test_connect_accept_and_inbox_happy_path(make_agent, signed_client):
    requester = make_agent(github_user_id=1)
    target = make_agent(github_user_id=2)
    session = FakeConnectionSession([requester, target])
    from_intent = session.add_intent(requester)
    target_intent = session.add_intent(target)
    app = _app_with_connection_overrides(session)

    async with signed_client(app, requester) as client:
        connect_response = await client.post(
            "/v1/connect",
            json=_connect_payload(target_intent, from_intent),
        )
    request_id = connect_response.json()["request_id"]

    async with signed_client(app, target) as client:
        inbox_response = await client.get("/v1/inbox")
        accept_response = await client.post(f"/v1/connect/{request_id}/accept")
        target_session_inbox = await client.get("/v1/inbox")

    async with signed_client(app, requester) as client:
        requester_session_inbox = await client.get("/v1/inbox")

    assert connect_response.status_code == 201
    assert requester.row.connection_requests_sent_total == 1
    assert target.row.connection_requests_received_total == 1
    assert inbox_response.json()["pending_requests"][0]["request_id"] == request_id
    assert (
        inbox_response.json()["pending_requests"][0]["from_intent"]["intent_id"]
        == from_intent.id
    )
    assert accept_response.status_code == 201
    assert target.row.connection_requests_accepted_total == 1
    session_id = accept_response.json()["session_id"]
    assert (
        target_session_inbox.json()["accepted_sessions_awaiting_join"][0]["session_id"]
        == session_id
    )
    assert (
        requester_session_inbox.json()["accepted_sessions_awaiting_join"][0]["session_id"]
        == session_id
    )


async def test_reject_flips_status_and_recently_rejected(make_agent, signed_client):
    requester = make_agent(github_user_id=1)
    target = make_agent(github_user_id=2)
    session = FakeConnectionSession([requester, target])
    from_intent = session.add_intent(requester)
    target_intent = session.add_intent(target)
    app = _app_with_connection_overrides(session)

    async with signed_client(app, requester) as client:
        connect_response = await client.post(
            "/v1/connect",
            json=_connect_payload(target_intent, from_intent),
        )
    request_id = connect_response.json()["request_id"]

    async with signed_client(app, target) as client:
        reject_response = await client.post(
            f"/v1/connect/{request_id}/reject",
            json={"reason": "not enough domain overlap"},
        )
        target_inbox = await client.get("/v1/inbox")

    async with signed_client(app, requester) as client:
        requester_inbox = await client.get("/v1/inbox")

    assert reject_response.status_code == 204
    assert session.requests[request_id].status == "rejected"
    assert target_inbox.json()["pending_requests"] == []
    assert requester_inbox.json()["recently_rejected"][0]["request_id"] == request_id
    assert requester_inbox.json()["recently_rejected"][0]["reason"] == "not enough domain overlap"


async def test_cancel_flips_status_and_removes_from_target_inbox(make_agent, signed_client):
    requester = make_agent(github_user_id=1)
    target = make_agent(github_user_id=2)
    session = FakeConnectionSession([requester, target])
    from_intent = session.add_intent(requester)
    target_intent = session.add_intent(target)
    app = _app_with_connection_overrides(session)

    async with signed_client(app, requester) as client:
        connect_response = await client.post(
            "/v1/connect",
            json=_connect_payload(target_intent, from_intent),
        )
        request_id = connect_response.json()["request_id"]
        cancel_response = await client.post(f"/v1/connect/{request_id}/cancel")

    async with signed_client(app, target) as client:
        target_inbox = await client.get("/v1/inbox")

    assert cancel_response.status_code == 204
    assert session.requests[request_id].status == "cancelled"
    assert target_inbox.json()["pending_requests"] == []


async def test_connect_send_limits_are_enforced(make_agent, signed_client):
    requester = make_agent(github_user_id=1)
    target = make_agent(github_user_id=2)
    session = FakeConnectionSession([requester, target])
    from_intent = session.add_intent(requester)
    target_intent = session.add_intent(target)
    app = _app_with_connection_overrides(session)

    session.pending_count = 20
    async with signed_client(app, requester) as client:
        pending_response = await client.post(
            "/v1/connect",
            json=_connect_payload(target_intent, from_intent),
        )

    session.pending_count = 0
    session.daily_count = 50
    async with signed_client(app, requester) as client:
        daily_response = await client.post(
            "/v1/connect",
            json=_connect_payload(target_intent, from_intent),
        )

    assert pending_response.status_code == 409
    assert pending_response.json()["error_code"] == "connection_request_limit_reached"
    assert daily_response.status_code == 429
    assert daily_response.json()["error_code"] == "rate_limited"


async def test_inbox_poll_updates_last_seen_at(make_agent, signed_client):
    agent = make_agent()
    session = FakeConnectionSession([agent])
    app = _app_with_connection_overrides(session)

    async with signed_client(app, agent) as client:
        response = await client.get("/v1/inbox")

    assert response.status_code == 200
    assert agent.row.last_seen_at is not None
