from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import babeltower.main as main_module
from babeltower.crypto import sign
from babeltower.models import Session
from babeltower.relay import MESSAGE_CAP, SessionManager, SessionState


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True


class FakeRelayDb:
    def __init__(self, session: Session):
        self.session = session
        self.commits = 0

    async def get(self, model, key):
        del model
        if key == self.session.id:
            return self.session
        return None

    async def commit(self):
        self.commits += 1


class FakeRelayFactory:
    def __init__(self, db: FakeRelayDb):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


class FakeAgent:
    id = "agt_a"


class FakeExecuteResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def __iter__(self):
        return iter(self.rows)


class FakeWebsocketDb:
    def __init__(self, agents, session):
        self.agents = agents
        self.session = session

    async def execute(self, statement):
        params = statement.compile().params
        pubkey = params.get("pubkey_1")
        if pubkey is not None:
            agent = next((agent for agent in self.agents if agent.pubkey == pubkey), None)
            return FakeExecuteResult(scalar=agent)
        return FakeExecuteResult(rows=[(agent.id, agent.pubkey) for agent in self.agents])

    async def get(self, model, key):
        del model
        if key == self.session.id:
            return self.session
        return None

    async def commit(self):
        pass


class FakeWebsocketFactory:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


def _db_session():
    now = datetime.now(timezone.utc)
    return Session(
        id="ses_relay",
        agent_a_id="agt_a",
        agent_b_id="agt_b",
        connection_request_id="req_relay",
        status="active",
        created_at=now,
        expires_at=now + timedelta(hours=72),
        message_count=0,
    )


def _state():
    return SessionState(
        session_id="ses_relay",
        member_ids={"agt_a", "agt_b"},
        pubkeys_by_agent_id={"agt_a": "pub_a", "agt_b": "pub_b"},
    )


def _message(number: int = 1):
    return json.dumps(
        {
            "type": "message",
            "session_id": "ses_relay",
            "timestamp": "2026-05-21T14:35:01Z",
            "body": {"n": number},
            "signature": "opaque",
        }
    )


def _hello(agent, session_id: str, private_key: bytes):
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "type": "hello",
        "agent_pubkey": agent.pubkey,
        "session_id": session_id,
        "timestamp": timestamp,
        "signature": sign(private_key, f"hello\n{session_id}\n{timestamp}".encode()),
    }


async def test_relay_buffers_until_counterparty_reconnects():
    db = FakeRelayDb(_db_session())
    manager = SessionManager(session_factory=FakeRelayFactory(db))
    sender_socket = FakeWebSocket()
    recipient_socket = FakeWebSocket()
    state = _state()
    state.sockets["agt_a"] = sender_socket
    manager._states[state.session_id] = state

    await manager._handle_frame(state, FakeAgent(), sender_socket, _message())
    assert len(state.buffers["agt_a"]) == 1
    assert recipient_socket.sent == []

    state.sockets["agt_b"] = recipient_socket
    await manager._flush_for_joined_agent(state, "agt_b")

    assert len(state.buffers["agt_a"]) == 0
    assert recipient_socket.sent[0]["body"] == {"n": 1}
    assert recipient_socket.sent[0]["from"] == "pub_a"


async def test_relay_message_cap_ends_session_on_51st_message():
    db_session = _db_session()
    db = FakeRelayDb(db_session)
    manager = SessionManager(session_factory=FakeRelayFactory(db))
    sender_socket = FakeWebSocket()
    recipient_socket = FakeWebSocket()
    state = _state()
    state.message_count = MESSAGE_CAP
    state.sockets = {"agt_a": sender_socket, "agt_b": recipient_socket}
    manager._states[state.session_id] = state

    await manager._handle_frame(state, FakeAgent(), sender_socket, _message())

    assert db_session.status == "closed"
    assert db_session.close_reason == "message_cap_reached"
    assert sender_socket.sent[0]["type"] == "session_ended"
    assert recipient_socket.sent[0]["type"] == "session_ended"
    assert sender_socket.closed is True
    assert recipient_socket.closed is True


def test_websocket_signed_hello_buffers_until_second_join(make_agent, monkeypatch):
    a = make_agent(github_user_id=1)
    b = make_agent(github_user_id=2)
    now = datetime.now(timezone.utc)
    session = Session(
        id="ses_ws",
        agent_a_id=a.row.id,
        agent_b_id=b.row.id,
        connection_request_id="req_ws",
        status="awaiting_join",
        created_at=now,
        expires_at=now + timedelta(hours=72),
        message_count=0,
    )
    db = FakeWebsocketDb([a.row, b.row], session)
    manager = SessionManager(session_factory=FakeWebsocketFactory(db))
    monkeypatch.setattr(main_module, "session_manager", manager)
    app = main_module.create_app()

    with TestClient(app) as client:
        with client.websocket_connect("/v1/session/ses_ws") as ws_a:
            ws_a.send_json(_hello(a.row, session.id, a.private_key))
            assert ws_a.receive_json()["type"] == "ready"
            ws_a.send_json(
                {
                    "type": "message",
                    "session_id": session.id,
                    "timestamp": "2026-05-21T14:35:01Z",
                    "body": {"kind": "prejoin"},
                    "signature": "opaque",
                }
            )

            with client.websocket_connect("/v1/session/ses_ws") as ws_b:
                ws_b.send_json(_hello(b.row, session.id, b.private_key))
                assert ws_b.receive_json()["type"] == "ready"
                buffered = ws_b.receive_json()

    assert session.status == "active"
    assert buffered["body"] == {"kind": "prejoin"}
    assert buffered["from"] == a.public_key


def test_websocket_bad_hello_closes_4401(make_agent, monkeypatch):
    a = make_agent(github_user_id=1)
    b = make_agent(github_user_id=2)
    now = datetime.now(timezone.utc)
    session = Session(
        id="ses_ws_bad",
        agent_a_id=a.row.id,
        agent_b_id=b.row.id,
        connection_request_id="req_ws_bad",
        status="awaiting_join",
        created_at=now,
        expires_at=now + timedelta(hours=72),
        message_count=0,
    )
    db = FakeWebsocketDb([a.row, b.row], session)
    manager = SessionManager(session_factory=FakeWebsocketFactory(db))
    monkeypatch.setattr(main_module, "session_manager", manager)
    app = main_module.create_app()

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/v1/session/ses_ws_bad") as ws:
                bad_hello = _hello(a.row, session.id, a.private_key)
                bad_hello["signature"] = "not-valid"
                ws.send_json(bad_hello)
                ws.receive_json()

    assert exc_info.value.code == 4401


async def test_relay_buffers_messages_for_disconnected_party_until_reconnect():
    """Phase 7 owner-review: agent disconnects mid-session and reconnects.

    Both parties are active, B disconnects, A keeps sending; once B
    reconnects with a fresh socket the buffered messages must flush in
    order and the active_sessions metric must not be touched (since the
    session never left ACTIVE).
    """
    db = FakeRelayDb(_db_session())
    manager = SessionManager(session_factory=FakeRelayFactory(db))

    a_sock = FakeWebSocket()
    b_sock1 = FakeWebSocket()
    state = _state()
    state.sockets = {"agt_a": a_sock, "agt_b": b_sock1}
    state.active_at = datetime.now(timezone.utc)
    state.active_metric_counted = True
    manager._states[state.session_id] = state

    # B's connection drops; receive loop calls _disconnect with B's socket.
    await manager._disconnect(state, "agt_b", b_sock1)
    assert "agt_b" not in state.sockets

    # A sends two messages while B is offline. Both must be buffered, and
    # the stale b_sock1 must not receive anything (would indicate a leak).
    await manager._handle_frame(state, FakeAgent(), a_sock, _message(1))
    await manager._handle_frame(state, FakeAgent(), a_sock, _message(2))
    assert len(state.buffers["agt_a"]) == 2
    assert b_sock1.sent == []

    # B reconnects with a fresh socket; the manager registers it and
    # flushes the buffered messages in order.
    b_sock2 = FakeWebSocket()
    state.sockets["agt_b"] = b_sock2
    await manager._flush_for_joined_agent(state, "agt_b")

    assert [frame["body"] for frame in b_sock2.sent] == [{"n": 1}, {"n": 2}]
    assert all(frame["from"] == "pub_a" for frame in b_sock2.sent)
    assert state.buffers["agt_a"] == []
    # Session was never closed by the reconnect; the active flag stays set.
    assert state.active_metric_counted is True
    assert state.closed is False


async def test_relay_join_closes_zombie_socket_when_agent_reconnects():
    """If the old socket is still in the state map when an agent reconnects
    (e.g. the previous loop never reached _disconnect because of a network
    blip), _join must close the stale socket before swapping in the new
    one. Otherwise the old socket would receive forwarded frames forever.
    """
    db = FakeRelayDb(_db_session())
    manager = SessionManager(session_factory=FakeRelayFactory(db))

    stale_sock = FakeWebSocket()
    other_sock = FakeWebSocket()
    state = _state()
    state.sockets = {"agt_a": stale_sock, "agt_b": other_sock}
    state.active_at = datetime.now(timezone.utc)
    state.active_metric_counted = True
    manager._states[state.session_id] = state

    async def _no_op_pubkeys(_session_row):
        return state.pubkeys_by_agent_id

    manager._load_pubkeys = _no_op_pubkeys  # type: ignore[assignment]

    class _AgentStub:
        id = "agt_a"

    new_sock = FakeWebSocket()
    await manager._join(new_sock, db.session, _AgentStub())

    assert state.sockets["agt_a"] is new_sock
    assert stale_sock.closed is True
    # The counterparty's socket must not be touched.
    assert state.sockets["agt_b"] is other_sock
    assert other_sock.closed is False
