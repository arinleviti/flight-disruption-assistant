# to turn the server on:
# cd C:\Users\alevi\Documents\GitHub\flight-disruption-assistant\backend
# uv run uvicorn backend.main:app --reload --reload-include "*.md" --port 8000

from dotenv import load_dotenv

load_dotenv()  # must run before importing anything that might read environment variables (Langfuse keys too)

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langfuse import propagate_attributes

from backend.agents.supervisor import answer_request
from backend.db.inventory import build_inventory_db
from backend.models.case_state import CaseState
from backend.models.chat import Message, ChatRequest, ChatResponse
from backend.observability import finish_turn, langfuse, start_turn
from backend.rag.knowledge_base import build_knowledge_base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts
    build_inventory_db()
    build_knowledge_base()
    yield
    # Runs once when the server stops: send any traces still waiting to Langfuse
    langfuse.shutdown()

app = FastAPI(lifespan=lifespan)

# session_id -> conversation history (user and assistant messages only)
conversations: dict[str, list[Message]] = {}
# session_id -> that conversation's cases, one per booking reference
case_files: dict[str, dict[str, CaseState]] = {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(request: ChatRequest) -> ChatResponse:

    history = conversations.get(request.session_id, [])
    # First message of a session: start with no cases; get_booking adds them
    cases = case_files.setdefault(request.session_id, {})

    # Start counting this turn (tokens, cost, tools, guards) for the summary in the UI
    start_turn()

    # One Langfuse trace per turn. Everything that happens inside (LLM calls, tools, sub-agents,
    # guards) is nested under it, and session_id groups all the turns of one conversation.
    with langfuse.start_as_current_observation(
        name="chat_turn",
        as_type="agent",
        input={"message": request.message, "bookings_in_case_file": list(cases)},
    ) as turn:
        with propagate_attributes(session_id=request.session_id, trace_name="chat_turn"):
            answer_str = answer_request(request.message, history, cases)
        turn.update(output=answer_str)
        trace_id = langfuse.get_current_trace_id()

    stats = finish_turn()

    # The link to this turn in Langfuse. None if Langfuse isn't configured (or can't be reached).
    trace_url = None
    if trace_id and os.getenv("LANGFUSE_PUBLIC_KEY"):
        try:
            trace_url = langfuse.get_trace_url(trace_id=trace_id)
        except Exception:
            trace_url = None

    history.append(Message(role="user", content=request.message))
    history.append(Message(role="assistant", content=answer_str))

    conversations[request.session_id] = history
    return ChatResponse(reply=answer_str, session_id=request.session_id, stats=stats, trace_url=trace_url)