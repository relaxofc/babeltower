async def test_health(client):
    response = await client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert "time" in body


async def test_server_info(client):
    response = await client.get("/v1/server/info")

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.1.0",
        "embedding_model": "voyage-4-lite",
        "embedding_dimensions": 1024,
        "max_intent_chars": 4500,
        "max_active_intents_per_agent": 10,
        "session_message_cap": 50,
        "session_duration_minutes": 30,
        "connection_request_ttl_hours": 72,
    }


async def test_metrics_exposes_custom_babeltower_metrics(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    body = response.text
    assert "babeltower_intents_created_total" in body
    assert "babeltower_searches_total" in body
    assert "babeltower_active_sessions" in body
    assert "babeltower_messages_relayed_total" in body
    assert "babeltower_matches_confirmed_total" in body
    assert "babeltower_soft_bans_applied_total" in body
