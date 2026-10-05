from typing import Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    """One message of the conversation history (what the passenger and the assistant said)."""
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: str


class ToolUse(BaseModel):
    """One tool call during a turn, for the summary in the chat UI."""
    agent: str          # who called it: supervisor, rebooking or compensation
    name: str           # e.g. get_booking, search_flights
    ok: bool            # False if the tool (or a guard) returned an error
    duration_ms: int


class GuardHit(BaseModel):
    """A guard that fired during a turn: code corrected or refused something the model did."""
    agent: str
    name: str           # e.g. booking_not_confirmed, invented_flight_in_reply
    detail: str


class TurnStats(BaseModel):
    """What happened during one turn, summed up: shown in the "Details" dropdown under a reply."""
    duration_ms: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0                           # 0.0 when litellm has no price for the model
    models: list[str] = Field(default_factory=list)  # the models that actually answered
    fallbacks: int = 0                               # calls answered by a fallback model
    tools: list[ToolUse] = Field(default_factory=list)
    guards: list[GuardHit] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    stats: TurnStats | None = None
    trace_url: str | None = None   # this turn in Langfuse; None if Langfuse isn't configured