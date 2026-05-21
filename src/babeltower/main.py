from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text

from babeltower import __version__
from babeltower.config import get_settings
from babeltower.db import async_session, close_engine
from babeltower.jobs import shutdown_scheduler, start_scheduler
from babeltower.logging import (
    configure_logging,
    sentry_before_send,
    structured_access_log_middleware,
)
from babeltower.rate_limit import limiter
from babeltower.relay import session_manager
from babeltower.routes import (
    agents,
    blocks,
    connections,
    inbox,
    intents,
    matches,
    meta,
    register,
    search,
    sessions,
)

try:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
except ImportError:  # pragma: no cover
    _rate_limit_exceeded_handler = None
    RateLimitExceeded = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    redis_client = None
    scheduler = None
    configure_logging(settings.log_level)

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            before_send=sentry_before_send,
        )

    if settings.env != "test":
        async with async_session() as session:
            await session.execute(text("SELECT 1"))

        redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        await redis_client.ping()
        app.state.redis = redis_client
        scheduler = start_scheduler()
        app.state.scheduler = scheduler

    yield

    await shutdown_scheduler(scheduler)
    if redis_client is not None:
        await redis_client.aclose()
    await close_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="BabelTower", version=__version__, lifespan=lifespan)
    app.state.limiter = limiter
    app.middleware("http")(structured_access_log_middleware)
    if RateLimitExceeded is not None and _rate_limit_exceeded_handler is not None:
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(meta.router, prefix="/v1", tags=["meta"])
    app.include_router(register.router, prefix="/v1", tags=["registration"])
    app.include_router(intents.router, prefix="/v1", tags=["intents"])
    app.include_router(search.router, prefix="/v1", tags=["search"])
    app.include_router(connections.router, prefix="/v1", tags=["connections"])
    app.include_router(inbox.router, prefix="/v1", tags=["inbox"])
    app.include_router(sessions.router, prefix="/v1", tags=["sessions"])
    app.include_router(matches.router, prefix="/v1", tags=["matches"])
    app.include_router(blocks.router, prefix="/v1", tags=["blocks"])
    app.include_router(agents.router, prefix="/v1", tags=["agents"])

    @app.websocket("/v1/session/{session_id}")
    async def websocket_session(websocket: WebSocket, session_id: str) -> None:
        await session_manager.handle_websocket(websocket, session_id)

    Instrumentator().instrument(app).expose(app, include_in_schema=False)
    return app


app = create_app()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
