from babeltower.db import get_session
from babeltower.embeddings import EMBEDDING_DIMENSIONS
from babeltower.main import create_app
from babeltower.routes.intents import get_embedder
from babeltower.schemas import SearchCandidate


class FakeSearchSession:
    def __init__(self, agents):
        self._agents = {agent.public_key: agent.row for agent in agents}
        self.candidates = []
        self.blocked_pairs = set()

    async def get_agent_by_pubkey(self, pubkey: str):
        return self._agents.get(pubkey)

    async def search_intents(self, *, agent, body, query_embedding):
        del query_embedding
        query = body.query_intent
        results = []
        for candidate in self.candidates:
            pair = (agent.id, candidate["agent_id"])
            reverse_pair = (candidate["agent_id"], agent.id)
            if candidate["agent_id"] == agent.id:
                continue
            if pair in self.blocked_pairs or reverse_pair in self.blocked_pairs:
                continue
            if candidate["status"] != "active":
                continue
            if candidate["match_type"] != query.match_type:
                continue
            if candidate["similarity"] < 0.70:
                continue
            filter_mismatch = any(
                str(candidate["filters"].get(key)) != str(value)
                for key, value in query.filters.items()
            )
            if filter_mismatch:
                continue
            results.append(candidate)

        results.sort(key=lambda item: item["similarity"], reverse=True)
        return [
            SearchCandidate(
                intent_id=item["intent_id"],
                agent_pubkey=item["agent_pubkey"],
                match_type=item["match_type"],
                seeking=item["seeking"],
                offering=item["offering"],
                constraints=item["constraints"],
                filters=item["filters"],
                similarity=item["similarity"],
                agent_status=item["agent_status"],
            )
            for item in results[: body.max_results]
        ]


def _search_payload(**query_overrides):
    query = {
        "match_type": "co-founder-technical",
        "seeking": "technical co-founder for biotech startup",
        "offering": "business founder with biotech network",
        "constraints": "Seoul",
        "filters": {"location": "Seoul"},
    }
    query.update(query_overrides)
    return {"query_intent": query, "max_results": 20}


def _candidate(agent, *, intent_id, similarity=0.82, **overrides):
    data = {
        "intent_id": intent_id,
        "agent_id": agent.row.id,
        "agent_pubkey": agent.public_key,
        "match_type": "co-founder-technical",
        "seeking": "technical co-founder for biotech startup",
        "offering": "ML engineer with biotech background",
        "constraints": "Seoul",
        "filters": {"location": "Seoul", "language": "en"},
        "similarity": similarity,
        "status": "active",
        "agent_status": "active",
    }
    data.update(overrides)
    return data


def _app_with_search_overrides(session):
    app = create_app()

    async def override_session():
        yield session

    async def fake_embedder(seeking, offering, constraints="", *, redis=None):
        return [0.01] * EMBEDDING_DIMENSIONS

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_embedder] = lambda: fake_embedder
    return app


async def test_search_returns_similar_results_sorted(make_agent, signed_client):
    requester = make_agent(github_user_id=1)
    a = make_agent(github_user_id=2)
    b = make_agent(github_user_id=3)
    session = FakeSearchSession([requester, a, b])
    session.candidates = [
        _candidate(a, intent_id="int_a", similarity=0.80),
        _candidate(b, intent_id="int_b", similarity=0.91),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert response.status_code == 200
    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_b", "int_a"]


async def test_search_excludes_own_intents(make_agent, signed_client):
    requester = make_agent()
    other = make_agent(github_user_id=2)
    session = FakeSearchSession([requester, other])
    session.candidates = [
        _candidate(requester, intent_id="int_own", similarity=0.99),
        _candidate(other, intent_id="int_other", similarity=0.80),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_other"]


async def test_search_excludes_blocked_agents_both_directions(make_agent, signed_client):
    requester = make_agent()
    blocked_by_requester = make_agent(github_user_id=2)
    blocks_requester = make_agent(github_user_id=3)
    visible = make_agent(github_user_id=4)
    session = FakeSearchSession([requester, blocked_by_requester, blocks_requester, visible])
    session.blocked_pairs = {
        (requester.row.id, blocked_by_requester.row.id),
        (blocks_requester.row.id, requester.row.id),
    }
    session.candidates = [
        _candidate(blocked_by_requester, intent_id="int_blocked_a"),
        _candidate(blocks_requester, intent_id="int_blocked_b"),
        _candidate(visible, intent_id="int_visible"),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_visible"]


async def test_search_excludes_inactive_statuses(make_agent, signed_client):
    requester = make_agent()
    dormant = make_agent(github_user_id=2)
    expired = make_agent(github_user_id=3)
    deleted = make_agent(github_user_id=4)
    active = make_agent(github_user_id=5)
    session = FakeSearchSession([requester, dormant, expired, deleted, active])
    session.candidates = [
        _candidate(dormant, intent_id="int_dormant", status="dormant"),
        _candidate(expired, intent_id="int_expired", status="expired"),
        _candidate(deleted, intent_id="int_deleted", status="deleted"),
        _candidate(active, intent_id="int_active"),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_active"]


async def test_search_filter_equality_enforced(make_agent, signed_client):
    requester = make_agent()
    seoul = make_agent(github_user_id=2)
    berlin = make_agent(github_user_id=3)
    session = FakeSearchSession([requester, seoul, berlin])
    session.candidates = [
        _candidate(seoul, intent_id="int_seoul", filters={"location": "Seoul"}),
        _candidate(berlin, intent_id="int_berlin", filters={"location": "Berlin"}),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post(
            "/v1/search",
            json=_search_payload(filters={"location": "Seoul"}),
        )

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_seoul"]


async def test_search_threshold_respected(make_agent, signed_client):
    requester = make_agent()
    similar = make_agent(github_user_id=2)
    dissimilar = make_agent(github_user_id=3)
    session = FakeSearchSession([requester, similar, dissimilar])
    session.candidates = [
        _candidate(similar, intent_id="int_similar", similarity=0.70),
        _candidate(dissimilar, intent_id="int_dissimilar", similarity=0.69),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_similar"]


async def test_search_match_type_exact_match(make_agent, signed_client):
    requester = make_agent()
    technical = make_agent(github_user_id=2)
    business = make_agent(github_user_id=3)
    session = FakeSearchSession([requester, technical, business])
    session.candidates = [
        _candidate(technical, intent_id="int_technical", match_type="co-founder-technical"),
        _candidate(business, intent_id="int_business", match_type="co-founder-business"),
    ]
    app = _app_with_search_overrides(session)

    async with signed_client(app, requester) as client:
        response = await client.post("/v1/search", json=_search_payload())

    assert [item["intent_id"] for item in response.json()["candidates"]] == ["int_technical"]
