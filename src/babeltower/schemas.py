from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    version: str
    time: datetime


class ServerInfoResponse(BaseModel):
    version: str
    embedding_model: str
    embedding_dimensions: int
    max_intent_chars: int
    max_active_intents_per_agent: int
    session_message_cap: int
    session_duration_minutes: int
    connection_request_ttl_hours: int


class ErrorResponse(BaseModel):
    error_code: str
    detail: Optional[str] = None


class RegistrationInitRequest(BaseModel):
    agent_pubkey: str
    nonce: str
    nonce_signature: str


class RegistrationInitResponse(BaseModel):
    registration_token: str
    github_oauth_url: str
    expires_in: int = 600


class RegistrationStatusResponse(BaseModel):
    status: str
    agent_pubkey: Optional[str] = None
    registered_at: Optional[datetime] = None
    reason: Optional[str] = None


class IntentCreateRequest(BaseModel):
    # match_type pattern is enforced inside the route so violations return
    # 400 + error_code: invalid_match_type per PROTOCOL.md §6.2, rather
    # than Pydantic's default 422.
    match_type: str = Field(max_length=64)
    seeking: str = Field(max_length=2000)
    offering: str = Field(max_length=2000)
    constraints: str = Field(default="", max_length=500)
    filters: dict[str, Any] = Field(default_factory=dict)
    ttl_days: int = Field(default=30, ge=1, le=90)


class IntentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    intent_id: str
    agent_pubkey: Optional[str] = None
    match_type: str
    seeking: str
    offering: str
    constraints: str
    filters: dict[str, Any]
    ttl_days: int
    created_at: datetime
    expires_at: datetime
    status: str


class SearchQueryIntent(BaseModel):
    match_type: str = Field(max_length=64, pattern=r"^[a-z0-9-]{1,64}$")
    seeking: str = Field(max_length=2000)
    offering: str = Field(max_length=2000)
    constraints: str = Field(default="", max_length=500)
    filters: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query_intent: SearchQueryIntent
    max_results: int = Field(default=20, ge=1, le=20)


class SearchCandidate(BaseModel):
    intent_id: str
    agent_pubkey: str
    match_type: str
    seeking: str
    offering: str
    constraints: str
    filters: dict[str, Any]
    similarity: float
    agent_status: str


class SearchResponse(BaseModel):
    candidates: list[SearchCandidate]


class ConnectionCreateRequest(BaseModel):
    target_intent_id: str
    from_intent_id: str
    opening_message: Optional[str] = Field(default=None, max_length=500)


class ConnectionCreateResponse(BaseModel):
    request_id: str
    target_agent_pubkey: str
    status: str
    expires_at: datetime


class SessionAcceptResponse(BaseModel):
    session_id: str
    ws_url: str
    expires_at: datetime


class RejectConnectionRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)


class InboxPendingRequest(BaseModel):
    request_id: str
    from_agent_pubkey: str
    from_intent: IntentResponse
    target_intent_id: str
    opening_message: Optional[str] = None
    received_at: datetime
    expires_at: datetime


class InboxSession(BaseModel):
    session_id: str
    counterparty_pubkey: str
    accepted_at: datetime
    session_expires_at: datetime


class InboxMatchProposal(BaseModel):
    session_id: str
    proposed_by: str
    proposed_at: datetime


class InboxMatchedHandoff(BaseModel):
    session_id: str
    counterparty_pubkey: str
    matched_at: datetime


class InboxRejectedRequest(BaseModel):
    request_id: str
    rejected_at: datetime
    reason: Optional[str] = None


class InboxResponse(BaseModel):
    pending_requests: list[InboxPendingRequest]
    accepted_sessions_awaiting_join: list[InboxSession]
    match_proposals: list[InboxMatchProposal]
    matched_handoffs: list[InboxMatchedHandoff]
    recently_rejected: list[InboxRejectedRequest] = Field(default_factory=list)


class MatchSessionRequest(BaseModel):
    session_id: str


class MatchRejectRequest(MatchSessionRequest):
    reason: Optional[str] = None


class MatchProposeResponse(BaseModel):
    session_id: str
    match_status: str
    proposed_by: str


class MatchAcceptResponse(BaseModel):
    session_id: str
    match_status: str
    confirmed_at: datetime


class MatchRejectResponse(BaseModel):
    session_id: str
    match_status: str


class BlockRequest(BaseModel):
    target_agent_pubkey: str
    reason: Optional[str] = Field(default=None, max_length=200)


class BlockResponse(BaseModel):
    target_agent_pubkey: str
    reason: Optional[str] = None
    created_at: datetime


class BlocksResponse(BaseModel):
    blocks: list[BlockResponse]
