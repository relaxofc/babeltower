from __future__ import annotations

from datetime import UTC, datetime, timedelta

from babeltower.db import get_session
from babeltower.main import create_app
from babeltower.models import Block, Session, new_id
from babeltower.schemas import BlockResponse


class FakeBlockSession:
    def __init__(self, agents):
        self._agents = {agent.public_key: agent.row for agent in agents}
        self.agents_by_id = {agent.row.id: agent.row for agent in agents}
        self.blocks = {}
        self.sessions = {}
        self.commits = 0

    async def get_agent_by_pubkey(self, pubkey: str):
        return self._agents.get(pubkey)

    def add_session(self, a, b, *, status: str = "active"):
        now = datetime.now(UTC)
        session = Session(
            id=new_id("ses"),
            agent_a_id=a.row.id,
            agent_b_id=b.row.id,
            connection_request_id=new_id("req"),
            status=status,
            created_at=now,
            expires_at=now + timedelta(hours=72),
            message_count=0,
        )
        self.sessions[session.id] = session
        return session

    async def upsert_block(self, blocker, blocked, reason, now):
        key = (blocker.id, blocked.id)
        block = self.blocks.get(key)
        created = block is None
        if block is None:
            block = Block(
                id=new_id("blk"),
                blocker_id=blocker.id,
                blocked_id=blocked.id,
                reason=reason,
                created_at=now,
            )
            self.blocks[key] = block
            blocked.blocks_received_total = (blocked.blocks_received_total or 0) + 1
        else:
            block.reason = reason
        return block, created

    async def close_sessions_between(self, a_id, b_id, now):
        closed = []
        for session in self.sessions.values():
            is_pair = {session.agent_a_id, session.agent_b_id} == {a_id, b_id}
            if is_pair and session.status != "closed":
                session.status = "closed"
                session.closed_at = now
                session.close_reason = "blocked"
                closed.append(session.id)
        return closed

    async def check_and_apply_soft_ban(self, agent_id, now=None):
        now = now or datetime.now(UTC)
        recent_blocks = [
            block
            for block in self.blocks.values()
            if block.blocked_id == agent_id and block.created_at >= now - timedelta(days=7)
        ]
        agent = self.agents_by_id[agent_id]
        if len(recent_blocks) > 5 and agent.status != "soft_banned":
            agent.status = "soft_banned"
            agent.soft_ban_lifts_at = now + timedelta(days=7)
            return True
        return False

    async def delete_block(self, blocker, target):
        self.blocks.pop((blocker.id, target.id), None)

    async def list_blocks(self, blocker):
        responses = []
        for block in self.blocks.values():
            if block.blocker_id == blocker.id:
                target = self.agents_by_id[block.blocked_id]
                responses.append(
                    BlockResponse(
                        target_agent_pubkey=target.pubkey,
                        reason=block.reason,
                        created_at=block.created_at,
                    )
                )
        return responses

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        del obj


def _app_with_block_overrides(session):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


async def test_block_upserts_lists_unblocks_and_closes_active_session(make_agent, signed_client):
    blocker = make_agent(github_user_id=1)
    blocked = make_agent(github_user_id=2)
    session = FakeBlockSession([blocker, blocked])
    active_session = session.add_session(blocker, blocked)
    app = _app_with_block_overrides(session)

    async with signed_client(app, blocker) as client:
        block_response = await client.post(
            "/v1/block",
            json={"target_agent_pubkey": blocked.public_key, "reason": "spam"},
        )
        list_response = await client.get("/v1/blocks")
        unblock_response = await client.delete(f"/v1/block/{blocked.public_key}")

    assert block_response.status_code == 204
    assert blocked.row.blocks_received_total == 1
    assert active_session.status == "closed"
    assert active_session.close_reason == "blocked"
    assert list_response.json()["blocks"][0]["target_agent_pubkey"] == blocked.public_key
    assert list_response.json()["blocks"][0]["reason"] == "spam"
    assert unblock_response.status_code == 204
    assert session.blocks == {}


async def test_sixth_block_soft_bans_target_but_read_still_works(make_agent, signed_client):
    blockers = [make_agent(github_user_id=index + 1) for index in range(6)]
    target = make_agent(github_user_id=20)
    bystander = make_agent(github_user_id=21)
    session = FakeBlockSession([*blockers, target, bystander])
    app = _app_with_block_overrides(session)

    for blocker in blockers:
        async with signed_client(app, blocker) as client:
            response = await client.post(
                "/v1/block",
                json={"target_agent_pubkey": target.public_key},
            )
            assert response.status_code == 204

    async with signed_client(app, target) as client:
        read_response = await client.get("/v1/blocks")
        write_response = await client.post(
            "/v1/block",
            json={"target_agent_pubkey": bystander.public_key},
        )

    assert target.row.status == "soft_banned"
    assert target.row.soft_ban_lifts_at is not None
    assert target.row.blocks_received_total == 6
    assert read_response.status_code == 200
    assert write_response.status_code == 403
