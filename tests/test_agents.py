from datetime import datetime, timezone

import pytest

from babeltower.main import create_app
from babeltower.routes.agents import delete_agent_account


@pytest.mark.asyncio
async def test_delete_agent_account_uses_session_hook(make_agent) -> None:
    agent = make_agent().row
    deleted_at = datetime.now(timezone.utc)

    class FakeDeleteSession:
        def __init__(self):
            self.deleted = None

        async def delete_agent_account(self, target_agent, target_deleted_at):
            self.deleted = (target_agent, target_deleted_at)
            target_agent.status = "deleted"
            target_agent.github_user_id = None

    session = FakeDeleteSession()

    await delete_agent_account(session, agent, deleted_at)  # type: ignore[arg-type]

    assert session.deleted == (agent, deleted_at)
    assert agent.status == "deleted"
    assert agent.github_user_id is None


@pytest.mark.asyncio
async def test_delete_agent_endpoint_is_mounted(client) -> None:
    app = create_app()
    routes = {getattr(route, "path", "") for route in app.routes}

    assert "/v1/agent" in routes
