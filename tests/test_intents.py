from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from babeltower.db import get_session
from babeltower.embeddings import EMBEDDING_DIMENSIONS
from babeltower.main import create_app
from babeltower.models import Agent, Intent, new_id
from babeltower.routes.intents import get_embedder
from babeltower.schemas import IntentCreateRequest


class FakeIntentSession:
    def __init__(self, agents):
        self._agents = {agent.public_key: agent.row for agent in agents}
        self.intents = {}
        self.active_count = 0
        self.daily_count = 0

    async def get_agent_by_pubkey(self, pubkey: str):
        return self._agents.get(pubkey)

    async def count_active_intents(self, agent_id: str):
        return self.active_count

    async def count_intents_created_since(self, agent_id: str, since):
        return self.daily_count

    async def create_intent(
        self,
        *,
        agent: Agent,
        body: IntentCreateRequest,
        embedding,
        created_at,
    ):
        intent = Intent(
            id=new_id("int"),
            agent_id=agent.id,
            match_type=body.match_type,
            seeking=body.seeking,
            offering=body.offering,
            constraints=body.constraints,
            filters=body.filters,
            embedding=embedding,
            ttl_days=body.ttl_days,
            created_at=created_at,
            expires_at=created_at + timedelta(days=body.ttl_days),
            status="active",
        )
        self.intents[intent.id] = intent
        self.active_count += 1
        self.daily_count += 1
        agent.intents_created_total = (agent.intents_created_total or 0) + 1
        return intent

    async def get_visible_intent(self, agent: Agent, intent_id: str):
        intent = self.intents.get(intent_id)
        if intent is None:
            return None
        if intent.agent_id == agent.id:
            return intent
        # Tests may opt-in to counterparty visibility (simulating an active
        # connection request) by adding the agent's id to allowed_viewers.
        allowed = getattr(intent, "_allowed_viewers", set())
        if agent.id in allowed:
            return intent
        return None

    async def get_agent_pubkey_by_id(self, agent_id: str):
        for row in self._agents.values():
            if row.id == agent_id:
                return row.pubkey
        return None

    async def get_owned_intent(self, agent: Agent, intent_id: str):
        intent = self.intents.get(intent_id)
        if intent is None or intent.agent_id != agent.id:
            return None
        return intent

    async def list_reusable_owned_intents(self, agent: Agent):
        reusable = [
            intent
            for intent in self.intents.values()
            if intent.agent_id == agent.id and intent.status in {"active", "dormant"}
        ]
        return sorted(reusable, key=lambda intent: intent.created_at, reverse=True)

    async def delete_intent(self, intent: Intent):
        intent.status = "deleted"

    async def refresh_intent(self, intent: Intent, refreshed_at: datetime):
        intent.status = "active" if intent.status == "dormant" else intent.status
        intent.expires_at = refreshed_at + timedelta(days=intent.ttl_days)
        return intent


def _intent_payload(**overrides):
    payload = {
        "match_type": "co-founder-technical",
        "seeking": "technical co-founder for biotech startup",
        "offering": "ML engineer with biotech background",
        "constraints": "Seoul or remote",
        "filters": {"location": "Seoul", "language": "en"},
        "ttl_days": 30,
    }
    payload.update(overrides)
    return payload


def _app_with_intent_overrides(session):
    app = create_app()

    async def override_session():
        yield session

    async def fake_embedder(seeking, offering, constraints="", *, redis=None):
        return [0.01] * EMBEDDING_DIMENSIONS

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_embedder] = lambda: fake_embedder
    return app


async def test_create_intent_happy_path(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        response = await client.post("/v1/intents", json=_intent_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["intent_id"].startswith("int_")
    assert body["agent_pubkey"] == agent.public_key
    assert body["match_type"] == "co-founder-technical"
    assert body["status"] == "active"
    assert body["ttl_days"] == 30
    assert session.daily_count == 1


async def test_create_intent_rejects_blocked_content(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)
    cases = [
        ("seeking", "email me owner@example.com", "email"),
        ("offering", "call +1 415 555 0199", "phone"),
        ("constraints", "see example.ai", "url"),
        ("seeking", "dm @founderbot", "handle"),
    ]

    async with signed_client(app, agent) as client:
        for field, value, violation in cases:
            response = await client.post("/v1/intents", json=_intent_payload(**{field: value}))
            assert response.status_code == 400
            assert response.json()["error_code"] == "content_blocked"
            assert violation in response.json()["violations"]


async def test_create_intent_enforces_active_limit(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    session.active_count = 10
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        response = await client.post("/v1/intents", json=_intent_payload())

    assert response.status_code == 409
    assert response.json()["error_code"] == "intent_limit_reached"


async def test_create_intent_enforces_daily_limit(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    session.daily_count = 30
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        response = await client.post("/v1/intents", json=_intent_payload())

    assert response.status_code == 429
    assert response.json()["error_code"] == "rate_limited"


async def test_create_intent_rejects_invalid_match_type(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        response = await client.post("/v1/intents", json=_intent_payload(match_type="Bad_Type"))

    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_match_type"


async def test_get_intent_owner_can_see_other_agent_gets_404(make_agent, signed_client):
    owner = make_agent(github_user_id=1)
    other = make_agent(github_user_id=2)
    session = FakeIntentSession([owner, other])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, owner) as client:
        create_response = await client.post("/v1/intents", json=_intent_payload())
        intent_id = create_response.json()["intent_id"]
        owner_response = await client.get(f"/v1/intents/{intent_id}")

    async with signed_client(app, other) as client:
        other_response = await client.get(f"/v1/intents/{intent_id}")

    assert owner_response.status_code == 200
    assert other_response.status_code == 404
    # The owner fetching their own intent must see their own pubkey.
    assert owner_response.json()["agent_pubkey"] == owner.public_key


async def test_get_intent_counterparty_visible_returns_owner_pubkey(make_agent, signed_client):
    """Regression: when a counterparty fetches an intent that's visible to
    them (via an active/pending session), the response must carry the
    *owner's* pubkey, not the requester's. Previously _to_response used
    the caller's pubkey, which misattributed every cross-agent intent
    fetch to the requester themself."""
    owner = make_agent(github_user_id=1)
    other = make_agent(github_user_id=2)
    session = FakeIntentSession([owner, other])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, owner) as client:
        create_response = await client.post("/v1/intents", json=_intent_payload())
        intent_id = create_response.json()["intent_id"]

    # Simulate an active connection request making the intent visible to `other`.
    session.intents[intent_id]._allowed_viewers = {other.row.id}

    async with signed_client(app, other) as client:
        response = await client.get(f"/v1/intents/{intent_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent_id"] == intent_id
    assert payload["agent_pubkey"] == owner.public_key, (
        "intent must be attributed to its owner, not the requesting counterparty"
    )
    assert payload["agent_pubkey"] != other.public_key


async def test_get_my_intents_returns_owned_active_and_dormant_only(make_agent, signed_client):
    owner = make_agent(github_user_id=1)
    other = make_agent(github_user_id=2)
    session = FakeIntentSession([owner, other])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, owner) as client:
        active_response = await client.post("/v1/intents", json=_intent_payload())
        dormant_response = await client.post(
            "/v1/intents",
            json=_intent_payload(seeking="dormant biotech founder intent"),
        )
        deleted_response = await client.post(
            "/v1/intents",
            json=_intent_payload(seeking="deleted biotech founder intent"),
        )
        expired_response = await client.post(
            "/v1/intents",
            json=_intent_payload(seeking="expired biotech founder intent"),
        )
        matched_response = await client.post(
            "/v1/intents",
            json=_intent_payload(seeking="matched biotech founder intent"),
        )

    async with signed_client(app, other) as client:
        other_response = await client.post("/v1/intents", json=_intent_payload())

    dormant_id = dormant_response.json()["intent_id"]
    deleted_id = deleted_response.json()["intent_id"]
    expired_id = expired_response.json()["intent_id"]
    matched_id = matched_response.json()["intent_id"]
    session.intents[dormant_id].status = "dormant"
    session.intents[deleted_id].status = "deleted"
    session.intents[expired_id].status = "expired"
    session.intents[matched_id].status = "matched"

    async with signed_client(app, owner) as client:
        response = await client.get("/v1/intents/mine")

    assert response.status_code == 200
    payload = response.json()
    assert {item["intent_id"] for item in payload["intents"]} == {
        active_response.json()["intent_id"],
        dormant_id,
    }
    assert {item["status"] for item in payload["intents"]} == {"active", "dormant"}
    assert all(item["agent_pubkey"] == owner.public_key for item in payload["intents"])
    assert other_response.json()["intent_id"] not in {
        item["intent_id"] for item in payload["intents"]
    }


async def test_delete_intent_sets_deleted(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        create_response = await client.post("/v1/intents", json=_intent_payload())
        intent_id = create_response.json()["intent_id"]
        delete_response = await client.delete(f"/v1/intents/{intent_id}")

    assert delete_response.status_code == 204
    assert session.intents[intent_id].status == "deleted"


async def test_refresh_intent_extends_expiry_and_reactivates_dormant(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        create_response = await client.post("/v1/intents", json=_intent_payload())
        intent_id = create_response.json()["intent_id"]
        old_expires_at = datetime.fromisoformat(
            create_response.json()["expires_at"].replace("Z", "+00:00")
        )
        session.intents[intent_id].status = "dormant"
        session.intents[intent_id].expires_at = datetime.now(UTC)
        response = await client.post(f"/v1/intents/{intent_id}/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    new_expires_at = datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00"))
    assert new_expires_at > old_expires_at - timedelta(seconds=5)


async def test_refresh_rejects_deleted_intent(make_agent, signed_client):
    agent = make_agent()
    session = FakeIntentSession([agent])
    app = _app_with_intent_overrides(session)

    async with signed_client(app, agent) as client:
        create_response = await client.post("/v1/intents", json=_intent_payload())
        intent_id = create_response.json()["intent_id"]
        session.intents[intent_id].status = "deleted"
        response = await client.post(f"/v1/intents/{intent_id}/refresh")

    assert response.status_code == 409


async def test_intents_live_stack_health_smoke():
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
