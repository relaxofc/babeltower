from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from babeltower.auth import MUTATION_AGENT_DEPENDENCY, SIGNED_AGENT_DEPENDENCY
from babeltower.main import create_app
from babeltower.models import Agent


def _app_with_signed_routes():
    app = create_app()

    @app.get("/v1/auth-check")
    async def signed_read(agent: Agent = SIGNED_AGENT_DEPENDENCY):
        return {"agent_pubkey": agent.pubkey}

    @app.post("/v1/auth-mutate-check")
    async def signed_write(agent: Agent = MUTATION_AGENT_DEPENDENCY):
        return {"agent_pubkey": agent.pubkey}

    return app


async def test_signed_request_passes(make_agent, signed_client, fake_session_override):
    agent = make_agent()
    app = _app_with_signed_routes()
    fake_session_override(app, agent)

    async with signed_client(app, agent) as client:
        response = await client.get("/v1/auth-check")

    assert response.status_code == 200
    assert response.json() == {"agent_pubkey": agent.public_key}


async def test_unsigned_request_fails_401(make_agent, fake_session_override):
    agent = make_agent()
    app = _app_with_signed_routes()
    fake_session_override(app, agent)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/auth-check")

    assert response.status_code == 401


async def test_bad_signature_fails_401(make_agent, signed_client, fake_session_override):
    agent = make_agent()
    app = _app_with_signed_routes()
    fake_session_override(app, agent)

    async with signed_client(app, agent) as client:
        response = await client.get("/v1/auth-check", headers={"X-Signature": "not-valid"})

    assert response.status_code == 401


async def test_expired_timestamp_fails_401(make_agent, signed_client, fake_session_override):
    agent = make_agent()
    app = _app_with_signed_routes()
    fake_session_override(app, agent)
    expired = (
        (datetime.now(timezone.utc) - timedelta(seconds=61))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    async with signed_client(app, agent) as client:
        response = await client.get("/v1/auth-check", headers={"X-Timestamp": expired})

    assert response.status_code == 401


async def test_unknown_pubkey_fails_401(make_agent, signed_client, fake_session_override):
    agent = make_agent()
    app = _app_with_signed_routes()
    fake_session_override(app)

    async with signed_client(app, agent) as client:
        response = await client.get("/v1/auth-check")

    assert response.status_code == 401


async def test_soft_banned_agent_fails_mutation_but_can_read(
    make_agent,
    signed_client,
    fake_session_override,
):
    agent = make_agent(status="soft_banned")
    app = _app_with_signed_routes()
    fake_session_override(app, agent)

    async with signed_client(app, agent) as client:
        read_response = await client.get("/v1/auth-check")
        write_response = await client.post("/v1/auth-mutate-check", json={"match_type": "x"})

    assert read_response.status_code == 200
    assert write_response.status_code == 403
