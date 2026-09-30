# to turn the server on: 
# cd C:\Users\alevi\Documents\GitHub\flight-disruption-assistant\backend
# uv run uvicorn backend.main:app --reload --port 8000

from fastapi import FastAPI
from backend.models.chat import Message, ChatRequest, ChatResponse
from backend.agents.supervisor import answer_request
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from backend.db.inventory import build_inventory_db

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts
    build_inventory_db()
    yield
    # Anything after yield runs once when the server stops (nothing needed yet)
    
app= FastAPI(lifespan=lifespan)

conversations: dict[str, list[Message]] = {}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/chat")
def chat(request: ChatRequest) -> ChatResponse:

    history = conversations.get(request.session_id, [])

    answer_str = answer_request(request.message, history)

    history.append(Message(role = "user", content = request.message))
    history.append(Message(role="assistant", content= answer_str))

    conversations[request.session_id] = history
    return ChatResponse(reply=answer_str, session_id=request.session_id)