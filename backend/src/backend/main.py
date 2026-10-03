# to turn the server on:
# cd C:\Users\alevi\Documents\GitHub\flight-disruption-assistant\backend
# uv run uvicorn backend.main:app --reload --port 8000

from dotenv import load_dotenv

load_dotenv()  # must run before importing anything that might read environment variables

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.agents.supervisor import answer_request
from backend.db.inventory import build_inventory_db
from backend.models.case_state import CaseState
from backend.models.chat import Message, ChatRequest, ChatResponse
from backend.rag.knowledge_base import build_knowledge_base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts
    build_inventory_db()
    build_knowledge_base()
    yield
    # Anything after yield runs once when the server stops (nothing needed yet)

app = FastAPI(lifespan=lifespan)

# session_id -> conversation history (user and assistant messages only)
conversations: dict[str, list[Message]] = {}
# session_id -> that conversation's cases, one per booking reference

#case_files = {
#    "test1": {                        # one conversation...
#        "AZX4K2": CaseState(...),     # ...with two cases
#        "BRT9Q7": CaseState(...),
#    },
#    "test2": { ... },                 # another conversation
#}
case_files: dict[str, dict[str, CaseState]] = {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def chat(request: ChatRequest) -> ChatResponse:

    history = conversations.get(request.session_id, [])
    # First message of a session: start with no cases; get_booking adds them
    cases = case_files.setdefault(request.session_id, {})
    answer_str = answer_request(request.message, history, cases)

    history.append(Message(role="user", content=request.message))
    history.append(Message(role="assistant", content=answer_str))

    conversations[request.session_id] = history
    return ChatResponse(reply=answer_str, session_id=request.session_id)