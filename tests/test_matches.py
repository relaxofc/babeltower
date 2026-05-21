from __future__ import annotations

from datetime import datetime, timedelta, timezone

from babeltower.db import get_session
from babeltower.main import create_app
from babeltower.models import Agent, Session


class FakeMatchSession:
    def __init__(self, agents):
        self._agents = {agent.public_key: agent.row for agent in agents}
        self.agents_by_id = {agent.row.id: agent.row for agent in agents}
        self.sessions = {}
        self.commits = 0

    async def get_agent_by_pubkey(self, pubkey: str):
        return self._agents.get(pubkey)

    def add_session(self, a, b, *, status: str = "active"):
        now = datetime.now(timezone.utc)
        session = Session(
            id="ses_test_match",
            agent_a_id=a.row.id,
            agent_b_id=b.row.id,
            connection_request_id="req_test_match",
            status=status,
            created_at=now,
            expires_at=now + timedelta(hours=72),
            message_count=0,
        )
        self.sessions[session.id] = session
        return session

    async def get_member_session(self, session_id: str, agent: Agent):
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if agent.id not in {session.agent_a_id, session.agent_b_id}:
            return None
        return session

    async def get_agent_pubkey_by_id(self, agent_id: str):
        return self.agents_by_id[agent_id].pubkey

    async def commit(self):
        self.commits += 1


def _app_with_match_overrides(session):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


async def test_match_propose_accept_confirms_session(make_agent, signed_client):
    proposer = make_agent(github_user_id=1)
    accepter = make_agent(github_user_id=2)
    session = FakeMatchSession([proposer, accepter])
    session_row = session.add_session(proposer, accepter)
    app = _app_with_match_overrides(session)

    async with signed_client(app, proposer) as client:
        propose_response = await client.post(
            "/v1/match/propose",
            json={"session_id": session_row.id},
        )

    async with signed_client(app, accepter) as client:
        accept_response = await client.post("/v1/match/accept", json={"session_id": session_row.id})

    assert propose_response.status_code == 200
    assert propose_response.json()["match_status"] == "proposed"
    assert session_row.status == "match_confirmed"
    assert session_row.match_proposed_by_id == proposer.row.id
    assert session_row.match_confirmed_at is not None
    assert accept_response.status_code == 200
    assert accept_response.json()["match_status"] == "confirmed"


async def test_match_reject_returns_to_active_and_can_repropose(make_agent, signed_client):
    proposer = make_agent(github_user_id=1)
    rejecter = make_agent(github_user_id=2)
    session = FakeMatchSession([proposer, rejecter])
    session_row = session.add_session(proposer, rejecter)
    app = _app_with_match_overrides(session)

    async with signed_client(app, proposer) as client:
        first_propose = await client.post("/v1/match/propose", json={"session_id": session_row.id})

    async with signed_client(app, rejecter) as client:
        reject_response = await client.post(
            "/v1/match/reject",
            json={"session_id": session_row.id, "reason": "need more discussion"},
        )

    async with signed_client(app, proposer) as client:
        second_propose = await client.post("/v1/match/propose", json={"session_id": session_row.id})

    assert first_propose.status_code == 200
    assert reject_response.status_code == 200
    assert session_row.status == "match_proposed"
    assert second_propose.status_code == 200
    assert session_row.match_proposed_by_id == proposer.row.id


async def test_proposer_cannot_accept_own_match(make_agent, signed_client):
    proposer = make_agent(github_user_id=1)
    other = make_agent(github_user_id=2)
    session = FakeMatchSession([proposer, other])
    session_row = session.add_session(proposer, other)
    app = _app_with_match_overrides(session)

    async with signed_client(app, proposer) as client:
        await client.post("/v1/match/propose", json={"session_id": session_row.id})
        accept_response = await client.post("/v1/match/accept", json={"session_id": session_row.id})

    assert accept_response.status_code == 403
    assert accept_response.json()["error_code"] == "proposer_cannot_accept"
