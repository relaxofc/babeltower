from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from fastapi import Request, Response

logger = logging.getLogger("babeltower.access")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))


def agent_pubkey_prefix(request: Request) -> str | None:
    agent = getattr(request.state, "agent", None)
    pubkey = getattr(agent, "pubkey", None)
    if pubkey:
        return pubkey[:8]

    header_pubkey = request.headers.get("X-Agent-Pubkey")
    if header_pubkey:
        return header_pubkey[:8]
    return None


def set_sentry_agent_tag(agent_prefix: str) -> None:
    try:
        import sentry_sdk
    except Exception:  # pragma: no cover - depends on optional runtime compatibility
        return
    sentry_sdk.set_tag("agent_pubkey_prefix", agent_prefix)


async def structured_access_log_middleware(
    request: Request,
    call_next: Callable,
) -> Response:
    started_at = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        agent_prefix = agent_pubkey_prefix(request)
        if agent_prefix:
            set_sentry_agent_tag(agent_prefix)
        logger.info(
            json.dumps(
                {
                    "event": "request",
                    "method": request.method,
                    "path": request.url.path,
                    "agent_pubkey": agent_prefix,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                },
                separators=(",", ":"),
            )
        )


def sentry_before_send(event, hint):
    exc_info = hint.get("exc_info") if hint else None
    if exc_info:
        exc = exc_info[1]
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and 400 <= status_code < 500:
            return None
    return event
