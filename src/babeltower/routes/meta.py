from datetime import datetime, timezone

from fastapi import APIRouter

from babeltower import __version__
from babeltower.schemas import HealthResponse, ServerInfoResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        time=datetime.now(timezone.utc),
    )


@router.get("/server/info", response_model=ServerInfoResponse)
async def server_info() -> ServerInfoResponse:
    return ServerInfoResponse(
        version=__version__,
        embedding_model="voyage-3",
        embedding_dimensions=1024,
        max_intent_chars=4500,
        max_active_intents_per_agent=10,
        session_message_cap=50,
        session_duration_minutes=30,
        connection_request_ttl_hours=72,
    )

