from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from babeltower import github_oauth
from babeltower.crypto import verify
from babeltower.db import get_session
from babeltower.models import Agent, new_id
from babeltower.schemas import (
    RegistrationInitRequest,
    RegistrationInitResponse,
    RegistrationStatusResponse,
)

router = APIRouter()
REGISTRATION_TOKEN_TTL_SECONDS = 600


def _redis_key(token: str) -> str:
    return f"regtok:{token}"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _decode_registration_state(raw: Optional[str]) -> Optional[dict]:
    if raw is None:
        return None
    return json.loads(raw)


async def get_redis(request: Request):
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return redis


REDIS_DEPENDENCY = Depends(get_redis)
SESSION_DEPENDENCY = Depends(get_session)


async def lock_github_user(session: AsyncSession, github_user_id: int) -> None:
    """Serialize concurrent registration callbacks for the same GitHub user.

    The 3-agents-per-GitHub-account cap (PROTOCOL.md §3.1) is the platform's
    primary sybil resistance. Without this lock, two callbacks landing at
    the same instant could each observe count<3 and both insert, blowing
    past the cap. pg_advisory_xact_lock takes a session-scoped lock keyed
    on the user id; it's released automatically at transaction commit.
    Test fakes can replace this with the no-op `lock_github_user` hook.
    """
    if hasattr(session, "lock_github_user"):
        await session.lock_github_user(github_user_id)  # type: ignore[attr-defined]
        return
    # Postgres pg_advisory_xact_lock requires a bigint; github user IDs
    # already fit so just pass through.
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(github_user_id)})


async def count_active_agents_for_github(session: AsyncSession, github_user_id: int) -> int:
    if hasattr(session, "count_active_agents_for_github"):
        return await session.count_active_agents_for_github(github_user_id)  # type: ignore[attr-defined]

    return await session.scalar(
        select(func.count())
        .select_from(Agent)
        .where(
            Agent.github_user_id == github_user_id,
            Agent.status.notin_(("deleted", "hard_banned")),
        )
    )


async def create_registered_agent(
    session: AsyncSession,
    *,
    agent_pubkey: str,
    github_user_id: int,
) -> Agent:
    if hasattr(session, "create_registered_agent"):
        return await session.create_registered_agent(  # type: ignore[attr-defined]
            agent_pubkey=agent_pubkey,
            github_user_id=github_user_id,
        )

    agent = Agent(pubkey=agent_pubkey, github_user_id=github_user_id)
    session.add(agent)
    await session.commit()
    return agent


@router.post("/register/init", response_model=RegistrationInitResponse)
async def init_registration(
    body: RegistrationInitRequest,
    redis=REDIS_DEPENDENCY,
) -> RegistrationInitResponse:
    try:
        nonce_bytes = base64.b64decode(body.nonce, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid nonce") from exc

    if not verify(body.agent_pubkey, nonce_bytes, body.nonce_signature):
        raise HTTPException(status_code=400, detail="invalid nonce signature")

    token = new_id("reg")
    state = {
        "status": "pending",
        "agent_pubkey": body.agent_pubkey,
        "github_oauth_state": token,
        "created_at": _utc_now_iso(),
    }
    await redis.setex(_redis_key(token), REGISTRATION_TOKEN_TTL_SECONDS, json.dumps(state))
    return RegistrationInitResponse(
        registration_token=token,
        github_oauth_url=github_oauth.build_oauth_url(token),
        expires_in=REGISTRATION_TOKEN_TTL_SECONDS,
    )


@router.get("/register/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    redis=REDIS_DEPENDENCY,
    session: AsyncSession = SESSION_DEPENDENCY,
) -> HTMLResponse:
    stored = _decode_registration_state(await redis.get(_redis_key(state)))
    if stored is None:
        raise HTTPException(status_code=400, detail="registration token expired")

    token_response = await github_oauth.exchange_code(code)
    github_user_id = await github_oauth.get_user_id(token_response["access_token"])

    # Serialize concurrent callbacks for this GitHub user before counting.
    # Released automatically at commit/rollback because it's a xact lock.
    await lock_github_user(session, github_user_id)
    agent_count = await count_active_agents_for_github(session, github_user_id)

    if agent_count >= 3:
        stored.update({"status": "failed", "reason": "github_account_at_agent_limit"})
        await redis.setex(_redis_key(state), REGISTRATION_TOKEN_TTL_SECONDS, json.dumps(stored))
        html = (
            "<html><body><h1>Registration failed</h1>"
            "<p>GitHub account is at agent limit.</p></body></html>"
        )
        return HTMLResponse(
            html,
            status_code=403,
        )

    await create_registered_agent(
        session,
        agent_pubkey=stored["agent_pubkey"],
        github_user_id=github_user_id,
    )
    stored.update(
        {
            "status": "complete",
            "github_user_id": github_user_id,
            "registered_at": _utc_now_iso(),
        }
    )
    await redis.setex(_redis_key(state), REGISTRATION_TOKEN_TTL_SECONDS, json.dumps(stored))
    html = "<html><body><h1>Registration complete</h1><p>Return to your agent.</p></body></html>"
    return HTMLResponse(html)


@router.get("/register/status", response_model=RegistrationStatusResponse)
async def registration_status(
    token: str,
    redis=REDIS_DEPENDENCY,
) -> RegistrationStatusResponse:
    stored = _decode_registration_state(await redis.get(_redis_key(token)))
    if stored is None:
        raise HTTPException(status_code=404, detail="registration token not found")

    if stored["status"] == "complete":
        return RegistrationStatusResponse(
            status="complete",
            agent_pubkey=stored["agent_pubkey"],
            registered_at=datetime.fromisoformat(stored["registered_at"].replace("Z", "+00:00")),
        )
    if stored["status"] == "failed":
        return RegistrationStatusResponse(status="failed", reason=stored.get("reason"))
    return RegistrationStatusResponse(status="pending")
