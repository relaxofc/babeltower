from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "test")

from babeltower.crypto import canonical_request_string, generate_keypair, sign  # noqa: E402
from babeltower.main import create_app  # noqa: E402
from babeltower.models import Agent  # noqa: E402
from babeltower.schemas import InboxResponse  # noqa: E402


@dataclass
class TestAgent:
    row: Agent
    private_key: bytes
    public_key: str


class SignedAsyncClient(AsyncClient):
    def __init__(self, *, agent: TestAgent, **kwargs):
        super().__init__(**kwargs)
        self._agent = agent

    async def request(self, method, url, **kwargs):
        auth = kwargs.pop("auth", self._auth)
        follow_redirects = kwargs.pop("follow_redirects", self.follow_redirects)
        stream = kwargs.pop("stream", False)
        request = self.build_request(method, url, **kwargs)

        timestamp = request.headers.get("X-Timestamp") or (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
        request.headers.setdefault("X-Agent-Pubkey", self._agent.public_key)
        request.headers.setdefault("X-Timestamp", timestamp)

        if "X-Signature" not in request.headers:
            canonical = canonical_request_string(
                request.method,
                request.url.raw_path.decode("ascii"),
                timestamp,
                request.content,
            )
            request.headers["X-Signature"] = sign(self._agent.private_key, canonical)

        return await self.send(request, auth=auth, follow_redirects=follow_redirects, stream=stream)


class FakeSession:
    def __init__(self, agents: dict[str, Agent]):
        self._agents = agents

    async def get_agent_by_pubkey(self, pubkey: str) -> Agent | None:
        return self._agents.get(pubkey)

    async def load_inbox(self, agent: Agent, now: datetime) -> InboxResponse:
        agent.last_seen_at = now
        return InboxResponse(
            pending_requests=[],
            accepted_sessions_awaiting_join=[],
            match_proposals=[],
            matched_handoffs=[],
            recently_rejected=[],
        )


@pytest.fixture
async def client() -> AsyncClient:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest.fixture
def make_agent():
    counter = 0

    def factory(status: str = "active", github_user_id: int = 1) -> TestAgent:
        nonlocal counter
        counter += 1
        private_key, public_key = generate_keypair()
        row = Agent(
            id=f"agt_test_{counter}_{status}",
            pubkey=public_key,
            github_user_id=github_user_id,
            status=status,
        )
        return TestAgent(row=row, private_key=private_key, public_key=public_key)

    return factory


@pytest.fixture
def signed_client():
    @asynccontextmanager
    async def factory(app, agent: TestAgent):
        transport = ASGITransport(app=app)
        async with SignedAsyncClient(
            agent=agent,
            transport=transport,
            base_url="http://test",
        ) as async_client:
            yield async_client

    return factory


@pytest.fixture
def fake_session_override():
    def factory(app, *agents: TestAgent):
        from babeltower.db import get_session

        session = FakeSession({agent.public_key: agent.row for agent in agents})

        async def override_get_session():
            yield session

        app.dependency_overrides[get_session] = override_get_session
        return session

    return factory
