from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketDisconnect

from babeltower.crypto import verify
from babeltower.db import async_session
from babeltower.metrics import active_sessions, messages_relayed_total
from babeltower.models import Agent, Session

HELLO_CLOSE_CODE = 4401
MESSAGE_CAP = 50
MAX_FRAME_BYTES = 16 * 1024
BUFFER_LIMIT = 10
INACTIVITY_WINDOW = timedelta(minutes=5)
ACTIVE_WINDOW = timedelta(minutes=30)
HANDOFF_WINDOW = timedelta(minutes=10)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> Optional[datetime]:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        return None
    return timestamp.astimezone(timezone.utc)


def _timestamp() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


_ERROR_MESSAGES: dict[str, str] = {
    "message_too_large": "Frame exceeds 16 KB; split into smaller messages.",
    "bad_json": "Frame is not valid JSON.",
    "unsupported_type": "Only type='message' is accepted after hello.",
    "session_mismatch": "session_id in envelope does not match this connection.",
    "session_closed": "Session is closed; reconnect not possible.",
    "buffer_full": "Counterparty is offline and the pre-join buffer is full.",
}


def _server_event(event_type: str, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if event_type == "error":
        # PROTOCOL.md §7.6 requires error frames to carry a human-readable
        # `message` alongside the machine-readable `code`. Fill in a default
        # if the caller did not pass one.
        code = body.get("code")
        if code is not None and "message" not in body:
            body = {**body, "message": _ERROR_MESSAGES.get(code, "Server error.")}
    return {
        "type": event_type,
        "session_id": session_id,
        "from": "server",
        "timestamp": _timestamp(),
        "body": body,
    }


@dataclass
class SessionState:
    session_id: str
    member_ids: set[str]
    pubkeys_by_agent_id: dict[str, str]
    sockets: dict[str, WebSocket] = field(default_factory=dict)
    buffers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    message_count: int = 0
    active_at: Optional[datetime] = None
    last_message_at: datetime = field(default_factory=utc_now)
    monitor_task: Optional[asyncio.Task] = None
    handoff_task: Optional[asyncio.Task] = None
    closed: bool = False
    active_metric_counted: bool = False


class SessionManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] = async_session,
        now_func=utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._now = now_func
        self._states: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._handoff_tasks: dict[str, asyncio.Task] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def has_session(self, session_id: str) -> bool:
        return session_id in self._states

    async def handle_websocket(self, websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        hello = await self._receive_hello(websocket, session_id)
        if hello is None:
            return

        agent_pubkey = hello["agent_pubkey"]
        auth = await self._authenticate_hello(agent_pubkey, session_id, hello)
        if auth is None:
            await websocket.close(code=HELLO_CLOSE_CODE)
            return
        session_row, agent = auth

        state = await self._join(websocket, session_row, agent)
        await websocket.send_json({"type": "ready", "session_id": session_id})
        await self._flush_for_joined_agent(state, agent.id)

        try:
            while True:
                raw = await websocket.receive_text()
                await self._handle_frame(state, agent, websocket, raw)
        except WebSocketDisconnect:
            await self._disconnect(state, agent.id, websocket)

    async def _receive_hello(
        self,
        websocket: WebSocket,
        session_id: str,
    ) -> Optional[dict[str, str]]:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return None
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            await websocket.close(code=HELLO_CLOSE_CODE)
            return None
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.close(code=HELLO_CLOSE_CODE)
            return None
        required = {"type", "agent_pubkey", "session_id", "timestamp", "signature"}
        if not required.issubset(hello) or hello.get("type") != "hello":
            await websocket.close(code=HELLO_CLOSE_CODE)
            return None
        if hello.get("session_id") != session_id:
            await websocket.close(code=HELLO_CLOSE_CODE)
            return None
        return hello

    async def _authenticate_hello(
        self,
        agent_pubkey: str,
        session_id: str,
        hello: dict[str, str],
    ) -> Optional[tuple[Session, Agent]]:
        timestamp = _parse_timestamp(hello["timestamp"])
        if timestamp is None:
            return None
        if abs((self._now() - timestamp).total_seconds()) > 60:
            return None
        canonical = f"hello\n{session_id}\n{hello['timestamp']}".encode()
        if not verify(agent_pubkey, canonical, hello["signature"]):
            return None

        async with self._session_factory() as db:
            result = await db.execute(select(Agent).where(Agent.pubkey == agent_pubkey))
            agent = result.scalar_one_or_none()
            session_row = await db.get(Session, session_id)
            if agent is None or session_row is None:
                return None
            if agent.status in {"soft_banned", "hard_banned", "deleted"}:
                return None
            if agent.id not in {session_row.agent_a_id, session_row.agent_b_id}:
                return None
            if session_row.status == "closed":
                return None
            return session_row, agent

    async def _join(self, websocket: WebSocket, session_row: Session, agent: Agent) -> SessionState:
        async with self._lock_for(session_row.id):
            state = self._states.get(session_row.id)
            if state is None:
                pubkeys = await self._load_pubkeys(session_row)
                state = SessionState(
                    session_id=session_row.id,
                    member_ids={session_row.agent_a_id, session_row.agent_b_id},
                    pubkeys_by_agent_id=pubkeys,
                    message_count=session_row.message_count or 0,
                )
                self._states[session_row.id] = state

            old_socket = state.sockets.get(agent.id)
            if old_socket is not None and old_socket is not websocket:
                await self._safe_close(old_socket)
            state.sockets[agent.id] = websocket
            state.buffers.setdefault(agent.id, [])

            if state.member_ids.issubset(state.sockets.keys()) and state.active_at is None:
                state.active_at = self._now()
                state.last_message_at = self._now()
                await self._set_session_active(session_row.id)
                active_sessions.inc()
                state.active_metric_counted = True
                state.monitor_task = asyncio.create_task(self._monitor_limits(state))
            return state

    async def _load_pubkeys(self, session_row: Session) -> dict[str, str]:
        async with self._session_factory() as db:
            result = await db.execute(
                select(Agent.id, Agent.pubkey).where(
                    Agent.id.in_((session_row.agent_a_id, session_row.agent_b_id))
                )
            )
            return {agent_id: pubkey for agent_id, pubkey in result}

    async def _set_session_active(self, session_id: str) -> None:
        async with self._session_factory() as db:
            session_row = await db.get(Session, session_id)
            if session_row is not None and session_row.status == "awaiting_join":
                now = self._now()
                session_row.status = "active"
                # Persist active_at and tighten expires_at to the 30-min
                # wall-clock deadline (PROTOCOL.md §7.4). Without this, the
                # session row keeps its 72h awaiting_join TTL and the
                # maintenance job has no way to clean up sessions whose
                # in-memory deadline task died across an API restart.
                session_row.active_at = now
                session_row.expires_at = now + ACTIVE_WINDOW
                agent_a = await db.get(Agent, session_row.agent_a_id)
                agent_b = await db.get(Agent, session_row.agent_b_id)
                if agent_a is not None:
                    agent_a.sessions_started_total = (agent_a.sessions_started_total or 0) + 1
                if agent_b is not None:
                    agent_b.sessions_started_total = (agent_b.sessions_started_total or 0) + 1
                await db.commit()

    async def _flush_for_joined_agent(self, state: SessionState, joined_agent_id: str) -> None:
        websocket = state.sockets.get(joined_agent_id)
        if websocket is None:
            return
        for sender_id in state.member_ids - {joined_agent_id}:
            buffered = state.buffers.get(sender_id, [])
            while buffered:
                await websocket.send_json(buffered.pop(0))

    async def _handle_frame(
        self,
        state: SessionState,
        agent: Agent,
        websocket: WebSocket,
        raw: str,
    ) -> None:
        if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
            await websocket.send_json(
                _server_event("error", state.session_id, {"code": "message_too_large"})
            )
            return
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            await websocket.send_json(
                _server_event("error", state.session_id, {"code": "bad_json"})
            )
            return
        if envelope.get("type") != "message":
            await websocket.send_json(
                _server_event("error", state.session_id, {"code": "unsupported_type"})
            )
            return
        if envelope.get("session_id") != state.session_id:
            await websocket.send_json(
                _server_event("error", state.session_id, {"code": "session_mismatch"})
            )
            return

        should_end_for_cap = False
        async with self._lock_for(state.session_id):
            if state.closed:
                await websocket.send_json(
                    _server_event("error", state.session_id, {"code": "session_closed"})
                )
                return
            if state.message_count >= MESSAGE_CAP:
                should_end_for_cap = True
                recipients = set()
            else:
                state.message_count += 1
                state.last_message_at = self._now()
                envelope["from"] = state.pubkeys_by_agent_id.get(agent.id, envelope.get("from"))
                if state.message_count % 10 == 0:
                    await self._persist_message_count(state.session_id, state.message_count)
                messages_relayed_total.inc()

                recipients = state.member_ids - {agent.id}

            for recipient_id in recipients:
                recipient_socket = state.sockets.get(recipient_id)
                if recipient_socket is not None:
                    await recipient_socket.send_json(envelope)
                    continue
                buffer = state.buffers.setdefault(agent.id, [])
                if len(buffer) >= BUFFER_LIMIT:
                    await websocket.send_json(
                        _server_event("error", state.session_id, {"code": "buffer_full"})
                    )
                else:
                    buffer.append(envelope)
        if should_end_for_cap:
            await self.end_session(state.session_id, "message_cap_reached")

    async def _persist_message_count(self, session_id: str, count: int) -> None:
        async with self._session_factory() as db:
            session_row = await db.get(Session, session_id)
            if session_row is not None:
                session_row.message_count = count
                await db.commit()

    async def _disconnect(
        self,
        state: SessionState,
        agent_id: str,
        websocket: WebSocket,
    ) -> None:
        async with self._lock_for(state.session_id):
            if state.sockets.get(agent_id) is websocket:
                del state.sockets[agent_id]

    async def _monitor_limits(self, state: SessionState) -> None:
        try:
            while not state.closed:
                await asyncio.sleep(5)
                now = self._now()
                if state.active_at and now - state.active_at >= ACTIVE_WINDOW:
                    await self.end_session(state.session_id, "time_limit_reached")
                    return
                if now - state.last_message_at >= INACTIVITY_WINDOW:
                    await self.end_session(state.session_id, "inactivity")
                    return
        except asyncio.CancelledError:
            return

    async def emit_to_counterparty(
        self,
        session_id: str,
        from_agent_id: str,
        event_type: str,
        body: dict[str, Any],
    ) -> None:
        state = self._states.get(session_id)
        if state is None:
            return
        event = _server_event(event_type, session_id, body)
        for agent_id in state.member_ids - {from_agent_id}:
            websocket = state.sockets.get(agent_id)
            if websocket is not None:
                await websocket.send_json(event)

    async def emit_to_session(self, session_id: str, event_type: str, body: dict[str, Any]) -> None:
        state = self._states.get(session_id)
        if state is None:
            return
        event = _server_event(event_type, session_id, body)
        for websocket in list(state.sockets.values()):
            await websocket.send_json(event)

    def schedule_handoff_close(self, session_id: str) -> None:
        state = self._states.get(session_id)
        if state is None:
            existing_task = self._handoff_tasks.get(session_id)
            if existing_task is not None:
                existing_task.cancel()
            self._handoff_tasks[session_id] = asyncio.create_task(
                self._close_after_handoff(session_id)
            )
            return
        if state.handoff_task is not None:
            state.handoff_task.cancel()
        state.handoff_task = asyncio.create_task(self._close_after_handoff(session_id))

    async def _close_after_handoff(self, session_id: str) -> None:
        try:
            await asyncio.sleep(HANDOFF_WINDOW.total_seconds())
            await self.end_session(session_id, "handoff_complete")
        except asyncio.CancelledError:
            return
        finally:
            self._handoff_tasks.pop(session_id, None)

    async def end_session(self, session_id: str, reason: str) -> None:
        state = self._states.get(session_id)
        if state is not None:
            async with self._lock_for(session_id):
                if state.closed:
                    return
                state.closed = True
                if state.monitor_task is not None:
                    state.monitor_task.cancel()
                if state.handoff_task is not None:
                    state.handoff_task.cancel()
                if state.active_metric_counted:
                    active_sessions.dec()
                    state.active_metric_counted = False
                await self._persist_closed(session_id, reason, state.message_count)
                event = _server_event("session_ended", session_id, {"reason": reason})
                for websocket in list(state.sockets.values()):
                    await self._safe_send_json(websocket, event)
                    await self._safe_close(websocket)
                state.sockets.clear()
            return

        await self._persist_closed(session_id, reason, None)

    async def _persist_closed(
        self,
        session_id: str,
        reason: str,
        message_count: Optional[int],
    ) -> None:
        async with self._session_factory() as db:
            session_row = await db.get(Session, session_id)
            if session_row is None:
                return
            session_row.status = "closed"
            session_row.closed_at = self._now()
            session_row.close_reason = reason
            if message_count is not None:
                session_row.message_count = message_count
            await db.commit()

    async def _safe_close(self, websocket: WebSocket) -> None:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass

    async def _safe_send_json(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        try:
            await websocket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass


session_manager = SessionManager()
