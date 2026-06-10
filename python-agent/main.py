import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from agent import get_graph, run_chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm up: create pool + run checkpointer.setup() at startup
    get_graph()
    yield


app = FastAPI(title="HCA Python Agent", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    thread_id: str


class ChatResponse(BaseModel):
    answer:        str
    cards:         list
    history_turns: int


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Message required")
    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_chat, req.message, req.thread_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
