#!/usr/bin/env python3
"""
PublicForward/ForVPS/vps_server.py — VPS bridge (FastAPI + WebSocket).

Exposes an OpenAI-compatible REST API (/v1/chat/completions, /v1/models) and
bridges requests over WebSocket to one or more local workers (public.py /
newpublic_BETA.py), each of which drives a pool of real DeepSeek browsers.

Run:
  python vps_server.py --port 8000 --token MY_SHARED_SECRET
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn


# --------------------------------------------------------------------------- #
# Config (env / CLI)
# --------------------------------------------------------------------------- #
AUTH_TOKEN = os.environ.get("PAF_TOKEN", "change-me")
REQUEST_TIMEOUT = float(os.environ.get("PAF_REQUEST_TIMEOUT", "330"))

MODEL_ALIASES = {
    # OpenAI-style model id -> (model_tab, deep_think)
    "deepseek-chat": {"model_tab": "instant", "deep_think": False},
    "deepseek-reasoner": {"model_tab": "expert", "deep_think": True},
    "deepseek-vision": {"model_tab": "vision", "deep_think": False},
}


# --------------------------------------------------------------------------- #
# Pydantic models — mirror the OpenAI Chat Completions schema
# --------------------------------------------------------------------------- #
class ChatMessage(BaseModel):
    role: str
    content: Any  # str, or list of content parts (for multimodal)
    name: Optional[str] = None


class ToolFunctionDef(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[dict] = None


class Tool(BaseModel):
    type: str = "function"
    function: ToolFunctionDef


class AttachmentPayload(BaseModel):
    """A file passed inline (base64) or by path on the worker."""
    name: Optional[str] = None
    mime: Optional[str] = None
    b64: Optional[str] = None
    path: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = "deepseek-chat"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[list[Tool]] = None
    # Custom DeepSeek extensions (non-OpenAI):
    deep_think: Optional[bool] = None
    web_search: Optional[bool] = None
    model_tab: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None  # "new" | "continue"
    attachments: Optional[list[AttachmentPayload]] = None

    def last_user_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == "user":
                if isinstance(msg.content, str):
                    return msg.content
                if isinstance(msg.content, list):
                    parts = [
                        p.get("text", "")
                        for p in msg.content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    return "\n".join(parts)
        return ""


# Resolve deferred (PEP 563) forward references for the pydantic models so they
# are fully built regardless of how this module is imported.
for _m in (ChatMessage, ToolFunctionDef, Tool, AttachmentPayload,
           ChatCompletionRequest):
    _m.model_rebuild()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _token_estimate(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _make_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------------------- #
# WorkerManager
# --------------------------------------------------------------------------- #
class WorkerEntry:
    def __init__(self, worker_id: str, ws: WebSocket, max_concurrent: int,
                 accounts: list[str]) -> None:
        self.worker_id = worker_id
        self.ws = ws
        self.max_concurrent = max_concurrent
        self.accounts = accounts
        self.in_flight = 0
        self.connected_at = time.time()


class WorkerManager:
    def __init__(self) -> None:
        self.workers: dict[str, WorkerEntry] = {}
        self._futures: dict[str, asyncio.Future] = {}
        self._session_worker: dict[str, str] = {}
        self._rr_index = 0
        self._lock = asyncio.Lock()

    # ----- registration ---------------------------------------------- #
    async def register(self, worker_id: str, ws: WebSocket,
                       max_concurrent: int, accounts: list[str]) -> None:
        async with self._lock:
            self.workers[worker_id] = WorkerEntry(
                worker_id, ws, max_concurrent, accounts
            )
        print(f"[vps] worker registered: {worker_id} accounts={accounts}")

    async def unregister(self, worker_id: str) -> None:
        async with self._lock:
            self.workers.pop(worker_id, None)
            # Drop session pins for this worker.
            for sid, wid in list(self._session_worker.items()):
                if wid == worker_id:
                    self._session_worker.pop(sid, None)
        print(f"[vps] worker unregistered: {worker_id}")

    def list_all_accounts(self) -> list[str]:
        out: list[str] = []
        for w in self.workers.values():
            out.extend(w.accounts)
        return out

    # ----- selection / load balancing -------------------------------- #
    def get_worker_for_task(self, session_id: Optional[str] = None
                            ) -> Optional[WorkerEntry]:
        # Session affinity for "continue" mode.
        if session_id and session_id in self._session_worker:
            wid = self._session_worker[session_id]
            if wid in self.workers:
                return self.workers[wid]

        candidates = [
            w for w in self.workers.values()
            if w.in_flight < w.max_concurrent
        ]
        if not candidates:
            # Fall back to least-busy even if at capacity.
            candidates = list(self.workers.values())
        if not candidates:
            return None
        # Least-busy, tie-broken round-robin.
        candidates.sort(key=lambda w: w.in_flight)
        min_busy = candidates[0].in_flight
        least = [w for w in candidates if w.in_flight == min_busy]
        self._rr_index = (self._rr_index + 1) % len(least)
        return least[self._rr_index]

    def release_task(self, worker_id: str, session_id: Optional[str] = None) -> None:
        w = self.workers.get(worker_id)
        if w and w.in_flight > 0:
            w.in_flight -= 1

    # ----- session binding ------------------------------------------- #
    def bind_session(self, session_id: str, worker_id: str) -> None:
        self._session_worker[session_id] = worker_id

    def unbind_session(self, session_id: str) -> None:
        self._session_worker.pop(session_id, None)

    def get_session_worker(self, session_id: str) -> Optional[str]:
        return self._session_worker.get(session_id)

    # ----- futures (match async WS reply to waiting HTTP request) ----- #
    def create_future(self, request_id: str) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._futures[request_id] = fut
        return fut

    def resolve_future(self, request_id: str, result: Any) -> None:
        fut = self._futures.pop(request_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    def reject_future(self, request_id: str, error: str) -> None:
        fut = self._futures.pop(request_id, None)
        if fut and not fut.done():
            fut.set_exception(RuntimeError(error))

    # ----- dispatch -------------------------------------------------- #
    async def dispatch(self, payload: dict, session_id: Optional[str]) -> dict:
        worker = self.get_worker_for_task(session_id)
        if worker is None:
            raise RuntimeError("No worker available")

        request_id = uuid.uuid4().hex
        fut = self.create_future(request_id)
        worker.in_flight += 1
        if session_id:
            self.bind_session(session_id, worker.worker_id)

        try:
            await worker.ws.send_text(json.dumps({
                "type": "task",
                "request_id": request_id,
                "payload": payload,
            }))
            result = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            self.reject_future(request_id, "worker timeout")
            raise RuntimeError("Worker timed out")
        finally:
            self.release_task(worker.worker_id, session_id)

    def stats(self) -> dict:
        return {
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "in_flight": w.in_flight,
                    "max_concurrent": w.max_concurrent,
                    "accounts": w.accounts,
                    "uptime_s": round(time.time() - w.connected_at, 1),
                }
                for w in self.workers.values()
            ],
            "total_accounts": len(self.list_all_accounts()),
            "bound_sessions": len(self._session_worker),
        }


manager = WorkerManager()


# --------------------------------------------------------------------------- #
# OpenAI-compatible response builders
# --------------------------------------------------------------------------- #
def _completion_json(model: str, content: str, prompt_text: str) -> dict:
    pt = _token_estimate(prompt_text)
    ct = _token_estimate(content)
    return {
        "id": _make_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


async def _sse_stream(model: str, content: str) -> AsyncGenerator[str, None]:
    """Emit OpenAI-style SSE chunks. (Worker returns full text; we chunk it.)"""
    cid = _make_id()
    created = int(time.time())

    def _chunk(delta: dict, finish: Optional[str] = None) -> str:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(obj)}\n\n"

    # Initial role chunk.
    yield _chunk({"role": "assistant"})
    # Chunk the content into reasonably sized pieces.
    step = 60
    for i in range(0, len(content), step):
        yield _chunk({"content": content[i:i + step]})
        await asyncio.sleep(0)  # cooperative yield
    yield _chunk({}, finish="stop")
    yield "data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[vps] server starting")
    yield
    # Shutdown: ask all workers to stop, drop stale state.
    for w in list(manager.workers.values()):
        try:
            await w.ws.send_text(json.dumps({"type": "shutdown"}))
        except Exception:
            pass
    print("[vps] server stopped")


app = FastAPI(title="PAF-ModelDeepSeek VPS", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# WebSocket: worker registration + task transport
# --------------------------------------------------------------------------- #
@app.websocket("/ws/worker")
async def ws_worker(ws: WebSocket) -> None:
    await ws.accept()
    worker_id: Optional[str] = None
    try:
        # First message must be a register frame with a valid token.
        first = json.loads(await ws.receive_text())
        if first.get("type") != "register" or first.get("token") != AUTH_TOKEN:
            await ws.send_text(json.dumps({"type": "error", "error": "unauthorized"}))
            await ws.close()
            return
        worker_id = first["worker_id"]
        await manager.register(
            worker_id, ws,
            int(first.get("max_concurrent", 1)),
            list(first.get("accounts", [])),
        )
        await ws.send_text(json.dumps({"type": "registered", "worker_id": worker_id}))

        while True:
            msg = json.loads(await ws.receive_text())
            mtype = msg.get("type")
            if mtype == "result":
                manager.resolve_future(msg["request_id"], msg.get("result"))
            elif mtype == "pong":
                continue
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[vps] worker ws error: {exc}")
    finally:
        if worker_id:
            await manager.unregister(worker_id)


# --------------------------------------------------------------------------- #
# REST endpoints
# --------------------------------------------------------------------------- #
@app.get("/")
async def root() -> dict:
    return {
        "service": "PAF-ModelDeepSeek VPS",
        "openai_compatible": True,
        "endpoints": ["/v1/chat/completions", "/v1/models", "/health"],
        "workers": len(manager.workers),
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "workers": len(manager.workers)}


@app.get("/v1/models")
async def list_models() -> dict:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": now, "owned_by": "paf-deepseek"}
            for mid in MODEL_ALIASES
        ],
    }


@app.get("/stats")
async def stats() -> dict:
    return manager.stats()


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not manager.workers:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "no workers connected",
                               "type": "service_unavailable"}},
        )

    prompt = req.last_user_message()
    alias = MODEL_ALIASES.get(req.model, MODEL_ALIASES["deepseek-chat"])
    model_tab = req.model_tab or alias["model_tab"]
    deep_think = req.deep_think if req.deep_think is not None else alias["deep_think"]
    web_search = bool(req.web_search) if req.web_search is not None else False
    mode = req.mode or ("continue" if req.session_id else "new")

    attachments = None
    if req.attachments:
        # Pass through path-based attachments (b64 ones would need a temp write
        # on the worker side; documented as TODO).
        attachments = [a.path for a in req.attachments if a.path]

    payload = {
        "prompt": prompt,
        "mode": mode,
        "model_tab": model_tab,
        "deep_think": deep_think,
        "web_search": web_search,
        "session_id": req.session_id,
        "attachments": attachments,
    }

    try:
        result = await manager.dispatch(payload, req.session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )

    if not result or not result.get("ok"):
        return JSONResponse(
            status_code=502,
            content={"error": {"message": result.get("error", "scrape failed")
                               if result else "no result",
                               "type": "upstream_error"}},
        )

    content = result.get("text", "")

    if req.stream:
        return StreamingResponse(
            _sse_stream(req.model, content),
            media_type="text/event-stream",
        )
    return JSONResponse(content=_completion_json(req.model, content, prompt))


# --------------------------------------------------------------------------- #
# Global exception handler — OpenAI-style error envelope
# --------------------------------------------------------------------------- #
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": {"message": str(exc), "type": "internal_error"}},
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    global AUTH_TOKEN
    p = argparse.ArgumentParser(description="PAF-ModelDeepSeek VPS server")
    p.add_argument("--port", type=int, default=int(os.environ.get("PAF_PORT", "8000")))
    p.add_argument("--host", default=os.environ.get("PAF_HOST", "0.0.0.0"))
    p.add_argument("--token", default=AUTH_TOKEN)
    args = p.parse_args()

    AUTH_TOKEN = args.token
    if AUTH_TOKEN == "change-me":
        print("[vps] WARNING: using default token 'change-me' — set --token!")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
