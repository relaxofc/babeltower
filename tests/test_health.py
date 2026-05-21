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
        "embedding_model": "voyage-3",
        "embedding_dimensions": 1024,
        "max_intent_chars": 4500,
        "max_active_intents_per_agent": 10,
        "session_message_cap": 50,
        "session_duration_minutes": 30,
        "connection_request_ttl_hours": 72,
    }

