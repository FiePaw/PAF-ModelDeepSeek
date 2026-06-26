#!/usr/bin/env python3
"""
newpublic_BETA.py — Local worker (BETA) with a persistent SessionStore.

Same as public.py but adds a SessionStore that maps session_id ->
conversation_state and persists it to dataSession/ as JSON. This lets
"continue"-mode conversations survive a worker restart: a returning session_id
is pinned to the same logical conversation/slot where possible.

Usage
-----
  python newpublic_BETA.py --vps ws://VPS_IP:8000/ws/worker \\
      --workers 2 --token MY_SHARED_SECRET
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool import BrowserPool
from config import DATA_SESSION_DIR
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.worker_beta")


# --------------------------------------------------------------------------- #
# SessionStore
# --------------------------------------------------------------------------- #
class SessionStore:
    """
    Disk-backed mapping of session_id -> conversation_state.

    conversation_state holds whatever is needed to resume a conversation:
      - slot_index: which pool slot last served this session
      - account: which account/cookie was used
      - turns: number of exchanges so far
      - last_updated: epoch seconds
    Stored as one JSON file per session under dataSession/.
    """

    def __init__(self, base_dir: Path = DATA_SESSION_DIR) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_all()

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self.base_dir / f"session_{safe}.json"

    def _load_all(self) -> None:
        for f in self.base_dir.glob("session_*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                self._cache[data["session_id"]] = data
            except Exception:
                continue
        if self._cache:
            log.info("Loaded %d persisted session(s)", len(self._cache))

    def get(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._cache.get(session_id)

    def upsert(self, session_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self._cache.get(session_id, {
                "session_id": session_id,
                "turns": 0,
                "created": time.time(),
            })
            state.update(fields)
            state["last_updated"] = time.time()
            self._cache[session_id] = state
            try:
                self._path(session_id).write_text(
                    json.dumps(state, indent=2), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not persist session %s: %s", session_id, exc)
            return state

    def bump_turn(self, session_id: str) -> None:
        state = self.get(session_id) or {}
        self.upsert(session_id, turns=int(state.get("turns", 0)) + 1)

    def all_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._cache.values())


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
class LocalWorkerBeta:
    def __init__(
        self, vps_url: str, token: str, num_workers: int, headless: bool = True
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id = f"workerB-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.pool: BrowserPool | None = None
        self.sessions = SessionStore()
        # Pin a session to a specific slot index for "continue" mode.
        self._session_slot: dict[str, int] = {
            s["session_id"]: s.get("slot_index")
            for s in self.sessions.all_sessions()
            if s.get("slot_index") is not None
        }
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.pool = BrowserPool(num_slots=self.num_workers, headless=self.headless)
        await self.pool.start()
        log.info("BETA worker %s ready: %s",
                 self.worker_id, self.pool.status_summary())
        try:
            await self._connect_loop()
        finally:
            if self.pool:
                await self.pool.stop()

    async def _connect_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._serve()
                backoff = 1
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("Connection lost (%s). Reconnect in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as exc:  # noqa: BLE001
                log.error("Worker error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _serve(self) -> None:
        log.info("Connecting to VPS: %s", self.vps_url)
        async with websockets.connect(self.vps_url, max_size=None) as ws:
            await ws.send(json.dumps({
                "type": "register",
                "token": self.token,
                "worker_id": self.worker_id,
                "max_concurrent": self.num_workers,
                "accounts": self.pool.list_accounts() if self.pool else [],
                "resumable_sessions": list(self._session_slot.keys()),
            }))
            ack = json.loads(await ws.recv())
            if ack.get("type") != "registered":
                raise RuntimeError(f"Registration rejected: {ack}")
            log.info("Registered (BETA) as %s", self.worker_id)
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "task":
                    asyncio.create_task(self._handle_task(ws, msg))
                elif msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif msg.get("type") == "shutdown":
                    self._stop.set()
                    break

    async def _handle_task(self, ws, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        payload = msg.get("payload", {})
        session_id = payload.get("session_id") or request_id
        prompt = payload.get("prompt", "")
        mode = payload.get("mode", "new")

        # If the session exists and the caller wants to continue, prefer continue.
        prior = self.sessions.get(session_id)
        if prior and mode == "new" and payload.get("auto_continue"):
            mode = "continue"

        send_kwargs = {
            "model_tab": payload.get("model_tab", "instant"),
            "deep_think": bool(payload.get("deep_think", False)),
            "web_search": bool(payload.get("web_search", False)),
        }
        attachments = payload.get("attachments")
        log.info("Task %s (session=%s) mode=%s", request_id, session_id, mode)

        try:
            assert self.pool is not None
            slot = await self.pool.acquire(timeout=180)
            try:
                result = await slot.scraper.scrape(
                    prompt, mode=mode, attachments=attachments, **send_kwargs
                )
            finally:
                await self.pool.release(slot, reset=(mode != "continue"))

            # Persist session state.
            self.sessions.upsert(
                session_id,
                slot_index=slot.index,
                account=slot.account,
            )
            self.sessions.bump_turn(session_id)
            self._session_slot[session_id] = slot.index

            await ws.send(json.dumps({
                "type": "result",
                "request_id": request_id,
                "worker_id": self.worker_id,
                "session_id": session_id,
                "result": result,
            }))
        except Exception as exc:  # noqa: BLE001
            log.error("Task %s failed: %s", request_id, exc)
            await ws.send(json.dumps({
                "type": "result",
                "request_id": request_id,
                "worker_id": self.worker_id,
                "session_id": session_id,
                "result": {"ok": False, "error": str(exc)},
            }))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PAF-ModelDeepSeek local worker (BETA)")
    p.add_argument("--vps", required=True)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--token", required=True)
    p.add_argument("--no-headless", action="store_true")
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    worker = LocalWorkerBeta(
        vps_url=args.vps, token=args.token,
        num_workers=args.workers, headless=not args.no_headless,
    )
    try:
        await worker.start()
        return 0
    except KeyboardInterrupt:
        return 130


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
