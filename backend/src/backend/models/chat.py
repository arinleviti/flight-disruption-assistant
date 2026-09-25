from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Message(BaseModel):
    #Literal means that the role field can only take one of the two specified string values: "user" or "assistant". This is similar to a TypeScript union type, where you can specify that a variable can only be one of a set of predefined values.
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    session_id: str

class ChatResponse(BaseModel):
    reply: str
    session_id: str