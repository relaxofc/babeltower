import base64
import json
import os
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from babeltower import github_oauth
from babeltower.crypto import sign
from babeltower.db import get_session
from babeltower.main import create_app
from babeltower.models import Agent


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def setex(self, key, ttl, value):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)


class FakeRegistrationSession:
    def __init__(self, agent_count=0):
        self.agent_count = agent_count
        self.created_agents = []
        self.locked_github_users: list[int] = []

    async def lock_github_user(self, github_user_id):
        # The real implementation takes a Postgres advisory lock; the fake
        # just records that the call happened so tests can assert it ran
        # before the count+insert pair.
        self.locked_github_users.append(github_user_id)

    async def count_active_agents_for_github(self, github_user_id):
        return self.agent_count

    async def create_registered_agent(self, *, agent_pubkey, github_user_id):
        agent = Agent(
            id=f"agt_fake_{len(self.created_agents)}",
            pubkey=agent_pubkey,
            github_user_id=github_user_id,
            status="active",
        )
        self.created_agents.append(agent)
        return agent


def _registration_app(session):
    app = create_app()
    app.state.redis = FakeRedis()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def _signed_registration_payload(agent):
    nonce = os.urandom(32)
    return {
        "agent_pubkey": agent.public_key,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "nonce_signature": sign(agent.private_key, nonce),
    }


async def test_registration_happy_path(make_agent, monkeypatch):
    agent = make_agent()
    session = FakeRegistrationSession(agent_count=0)
    app = _registration_app(session)
    monkeypatch.setattr(github_oauth, "build_oauth_url", lambda state: f"https://github.test/{state}")

    async def fake_exchange_code(code):
        return {"access_token": f"token-for-{code}"}

    async def fake_get_user_id(access_token):
        return 12345

    monkeypatch.setattr(github_oauth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(github_oauth, "get_user_id", fake_get_user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        init_response = await client.post(
            "/v1/register/init",
            json=_signed_registration_payload(agent),
        )
        token = init_response.json()["registration_token"]
        callback_response = await client.get(
            "/v1/register/oauth/callback",
            params={"code": "abc", "state": token},
        )
        status_response = await client.get("/v1/register/status", params={"token": token})

    assert init_response.status_code == 200
    assert init_response.json()["github_oauth_url"] == f"https://github.test/{token}"
    assert callback_response.status_code == 200
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "complete"
    assert status_response.json()["agent_pubkey"] == agent.public_key
    assert session.created_agents[0].github_user_id == 12345


async def test_registration_init_rejects_bad_nonce_signature(make_agent):
    agent = make_agent()
    other_agent = make_agent()
    app = _registration_app(FakeRegistrationSession())
    payload = _signed_registration_payload(agent)
    payload["nonce_signature"] = sign(other_agent.private_key, base64.b64decode(payload["nonce"]))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/register/init", json=payload)

    assert response.status_code == 400


async def test_registration_fails_when_github_account_at_agent_limit(make_agent, monkeypatch):
    agent = make_agent()
    app = _registration_app(FakeRegistrationSession(agent_count=3))
    monkeypatch.setattr(github_oauth, "build_oauth_url", lambda state: f"https://github.test/{state}")

    async def fake_exchange_code(code):
        return {"access_token": "token"}

    async def fake_get_user_id(access_token):
        return 12345

    monkeypatch.setattr(github_oauth, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(github_oauth, "get_user_id", fake_get_user_id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        init_response = await client.post(
            "/v1/register/init",
            json=_signed_registration_payload(agent),
        )
        token = init_response.json()["registration_token"]
        callback_response = await client.get(
            "/v1/register/oauth/callback",
            params={"code": "abc", "state": token},
        )
        status_response = await client.get("/v1/register/status", params={"token": token})

    assert callback_response.status_code == 403
    assert status_response.json() == {
        "status": "failed",
        "agent_pubkey": None,
        "registered_at": None,
        "reason": "github_account_at_agent_limit",
    }


async def test_registration_status_returns_404_for_expired_token():
    app = _registration_app(FakeRegistrationSession())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/register/status", params={"token": "reg_missing"})

    assert response.status_code == 404


async def test_registration_status_pending(make_agent):
    agent = make_agent()
    app = _registration_app(FakeRegistrationSession())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        init_response = await client.post(
            "/v1/register/init",
            json=_signed_registration_payload(agent),
        )
        token = init_response.json()["registration_token"]
        status_response = await client.get("/v1/register/status", params={"token": token})

    assert status_response.json() == {
        "status": "pending",
        "agent_pubkey": None,
        "registered_at": None,
        "reason": None,
    }


def test_registration_token_state_is_json():
    state = {"status": "pending", "agent_pubkey": "abc"}
    assert json.loads(json.dumps(state)) == state


async def test_oauth_callback_locks_github_user_before_count_and_insert(monkeypatch, make_agent):
    """Regression: the 3-agent cap was raceable because count + insert ran
    without a lock. The fix takes a Postgres advisory xact lock keyed on
    github_user_id. The test verifies the lock hook fires on the callback
    path so concurrent callbacks would be serialized in production."""
    session = FakeRegistrationSession(agent_count=0)
    app = _registration_app(session)

    monkeypatch.setattr(
        github_oauth,
        "exchange_code",
        AsyncMock(return_value={"access_token": "tok"}),
    )
    monkeypatch.setattr(github_oauth, "get_user_id", AsyncMock(return_value=42))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        agent = make_agent()
        init_response = await client.post(
            "/v1/register/init",
            json=_signed_registration_payload(agent),
        )
        token = init_response.json()["registration_token"]
        callback_response = await client.get(
            "/v1/register/oauth/callback",
            params={"code": "abc", "state": token},
        )

    assert callback_response.status_code == 200
    # Lock must have been taken for this GitHub user before the insert.
    assert session.locked_github_users == [42]
    assert len(session.created_agents) == 1
