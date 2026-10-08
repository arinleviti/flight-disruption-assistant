# to turn the server on:
# cd C:\Users\alevi\Documents\GitHub\flight-disruption-assistant\backend
# uv run uvicorn backend.main:app --reload --reload-include "*.md" --port 8000

from dotenv import load_dotenv

load_dotenv()  # must run before importing anything that might read environment variables (Langfuse keys too)

import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from langfuse import propagate_attributes

from backend.agents.supervisor import answer_request
from backend.db.inventory import build_inventory_db
from backend.models.case_state import CaseState
from backend.models.chat import Message, ChatRequest, ChatResponse
from backend.observability import finish_turn, langfuse, start_turn
from backend.rag.knowledge_base import build_knowledge_base

# Demo limits: a public demo must not be able to run up the model bill.
# The per-conversation limit can be dodged by starting a new conversation; the daily one can't.
MAX_MESSAGES_PER_CONVERSATION = 30
MAX_MESSAGES_PER_DAY = 500
LIMIT_REPLY = "This demo has reached its message limit. Please start a new conversation, or try again tomorrow."

# In the container (see the Dockerfile) the built React app is copied to /app/static.
# Locally this folder doesn't exist, so nothing is served from here and Vite serves the frontend as before.
STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


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

# session_id -> how many messages that conversation has sent
messages_per_session: dict[str, int] = {}
# messages sent today, by everyone: reset when the date changes
daily_count = {"date": "", "count": 0}


def over_demo_limit(session_id: str) -> bool:
    """Count this message, and say whether it goes over one of the demo limits."""
    today = date.today().isoformat()
    if daily_count["date"] != today:
        daily_count["date"] = today
        daily_count["count"] = 0
    daily_count["count"] += 1
    messages_per_session[session_id] = messages_per_session.get(session_id, 0) + 1

    return (
        daily_count["count"] > MAX_MESSAGES_PER_DAY
        or messages_per_session[session_id] > MAX_MESSAGES_PER_CONVERSATION
    )


@app.get("/health")
def health():
    return {"status": "ok"}

# Two paths for the same function: locally Vite forwards /api/... to the backend;
# in production there's no Vite, so the frontend calls /api/chat directly.
@app.post("/api/chat")
@app.post("/chat")
def chat(request: ChatRequest) -> ChatResponse:

    # Stop here, before any model is called, if the demo limits are reached
    if over_demo_limit(request.session_id):
        return ChatResponse(reply=LIMIT_REPLY, session_id=request.session_id)

    history = conversations.get(request.session_id, [])
    # First message of a session: start with no cases; get_booking adds them
    cases = case_files.setdefault(request.session_id, {})

    # Start counting this turn (tokens, cost, tools, guards) for the summary in the UI
    start_turn()

    # One Langfuse trace per turn. Everything that happens inside (LLM calls, tools, sub-agents,
    # guards) is nested under it, and session_id groups all the turns of one conversation.
    # with is used so that the trace is automatically closed when the turn ends, even if an exception occurs.
    # what is a trace? a trace is a record of one request, with all the events that happened during it. It can be viewed in the Langfuse dashboard.
    # start_as_current_observation(...) creates that object, and as turn stores it in a variable called turn.
    with langfuse.start_as_current_observation(
        name="chat_turn",
        as_type="agent",
        #When you pass a dictionary to list(), Python keeps only the keys and drops the values.
        input={"message": request.message, "bookings_in_case_file": list(cases)},
        #turn is the name of the trace, and it is used to group all the events that happen during this turn. It is also used to group all the turns of one conversation.
    ) as turn:
        with propagate_attributes(session_id=request.session_id, trace_name="chat_turn"):
            answer_str = answer_request(request.message, history, cases)
        turn.update(output=answer_str)
        trace_id = langfuse.get_current_trace_id()
    #this returns the updated stats with the time taken from the turn.
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
    #here stats in sent to the frontend.
    return ChatResponse(reply=answer_str, session_id=request.session_id, stats=stats, trace_url=trace_url)


# The website itself. This must come AFTER the routes above: "/" matches every path,
# so anything mounted before them would hide /api/chat and /health.
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")