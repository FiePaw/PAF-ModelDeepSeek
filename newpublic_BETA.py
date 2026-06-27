#!/usr/bin/env python3
"""
newpublic_BETA.py — Local worker (BETA) with full session persistence,
account pinning, slot pinning, auto-continue, and tool-result Turn 2 support.

This file supersedes the earlier BETA prototype. It now shares the production-
grade Session / SessionStore from public.py (TTL, disk restore, bump_turn,
cleanup_expired) and layers on the three BETA-exclusive features:

  1. auto_continue  — if ``mode=="new"`` but a session already exists for the
                      given ``session_id``, the worker silently promotes it to
                      ``mode="continue"`` (caller opt-in via ``auto_continue=true``).
  2. Slot pinning   — ``slot_index`` is persisted in the session so that a
                      returning CONTINUE request is routed to the same browser
                      slot (via ``pool.acquire_pinned()``), guaranteeing the
                      same persistent-profile login as Turn 1.
  3. Tool Turn 2    — if ``tool_messages`` is present in the payload together
                      with ``mode="continue"``, the worker calls
                      ``pool.run_task_with_tool_result()`` instead of the
                      normal scrape path, injecting structured
                      [TOOL RESULT] / [USER REQUEST] blocks into the live
                      conversation thread.

Usage
-----
  python newpublic_BETA.py --vps ws://VPS_IP:8000/ws/worker \\
      --workers 2 --token MY_SHARED_SECRET [--session-ttl 3600]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
import uuid
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool import BrowserPool
from config import ROTATION_CONFIG
# Reuse the production Session / SessionStore — no need to re-implement.
from public import Session, SessionStore, _SESSION_TTL
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.worker_beta")


# =========================================================================== #
# BetaSessionStore — extends SessionStore with slot_index pinning
# =========================================================================== #
class BetaSessionStore(SessionStore):
    """
    SessionStore extended with slot_index tracking for browser-slot pinning.

    Inherits all TTL / disk-restore / bump_turn / cleanup_expired behaviour
    from the production SessionStore. Adds one extra field per session:
      slot_index: int — the BrowserPool slot index that last served this session.
    """

    def upsert_slot(self, session_id: str, slot_index: int) -> None:
        """Record which pool slot served this session (for future pinning)."""
        s = self.get(session_id)
        if s is None:
            return
        # Piggyback slot_index into the JSON file via a temporary attribute;
        # Session is a dataclass so we use object.__setattr__ to avoid field
        # validation. We store it in the JSON as an extra key by overriding
        # _to_dict / _from_dict at the instance level via a subclass method.
        s._slot_index = slot_index  # type: ignore[attr-defined]
        self._save_slot_index(s)

    def _save_slot_index(self, s: Session) -> None:
        """Write the slot_index sidecar into the session JSON file."""
        try:
            path = self._path(s.session_id)
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            data["slot_index"] = getattr(s, "_slot_index", None)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("BetaSessionStore: failed to write slot_index: %s", exc)

    def get_slot_index(self, session_id: str) -> Optional[int]:
        """Return the last known slot_index for this session, or None."""
        try:
            path = self._path(session_id)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                val = data.get("slot_index")
                return int(val) if val is not None else None
        except Exception:
            return None


# =========================================================================== #
# LocalWorkerBeta
# =========================================================================== #
class LocalWorkerBeta:
    """
    BETA local worker.

    Extends the production LocalWorker with:
      - auto_continue promotion (new → continue)
      - slot-pinned pool.acquire_pinned() for CONTINUE
      - Tool Turn 2 via pool.run_task_with_tool_result()
      - Per-session asyncio.Lock (anti-collision)
      - Background cleanup (sessions + locks) every 60 s
    """

    def __init__(
        self,
        vps_url: str,
        token: str,
        num_workers: int,
        headless: bool = True,
        session_ttl: int = _SESSION_TTL,
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id = f"workerB-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.pool: Optional[BrowserPool] = None
        self._stop = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

        # Session store with TTL + slot pinning. Restore non-expired sessions
        # from disk immediately so pinned slots survive a restart.
        self.sessions = BetaSessionStore(ttl=session_ttl)
        self.sessions.load_from_disk()

        # Pre-populate slot map from restored sessions.
        self._session_slot: dict[str, int] = {}
        for s in self.sessions.all_sessions():
            idx = self.sessions.get_slot_index(s.session_id)
            if idx is not None:
                self._session_slot[s.session_id] = idx

        # Per-session asyncio.Lock — prevents two concurrent CONTINUE requests
        # to the same session from racing.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_meta: dict[str, float] = {}

        self._keepalive_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.pool = BrowserPool(num_slots=self.num_workers, headless=self.headless)
        await self.pool.start()
        log.info(
            "BETA worker %s ready: %s (sessions restored: %d)",
            self.worker_id,
            self.pool.status_summary(),
            len(self.sessions.all_sessions()),
        )
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
            except Exception as exc:
                log.error("Worker error: %s", exc, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _serve(self) -> None:
        log.info("Connecting to VPS: %s", self.vps_url)
        async with websockets.connect(self.vps_url, max_size=None) as ws:
            self._ws = ws
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

            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            self._cleanup_task   = asyncio.create_task(self._cleanup_loop())

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "task":
                    asyncio.create_task(self._handle_task(ws, msg))
                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong", "worker_id": self.worker_id}))
                elif mtype == "shutdown":
                    self._stop.set()
                    break

    # ------------------------------------------------------------------ #
    # Background tasks
    # ------------------------------------------------------------------ #
    async def _keepalive_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(30)
                if self._ws:
                    await self._ws.send(
                        json.dumps({"type": "ping", "worker_id": self.worker_id})
                    )
        except Exception:
            pass

    async def _cleanup_loop(self) -> None:
        """Remove expired sessions and idle locks every 60 s."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(60)
                cleaned = self.sessions.cleanup_expired()
                if cleaned:
                    log.debug("Cleanup: removed %d expired session(s)", cleaned)
                await self._cleanup_session_locks()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("Cleanup loop error: %s", exc)

    # ------------------------------------------------------------------ #
    # Per-session lock helpers
    # ------------------------------------------------------------------ #
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        self._session_locks_meta[session_id] = time.time()
        return self._session_locks[session_id]

    async def _cleanup_session_locks(self, ttl: float = 3600.0) -> None:
        """GC per-session locks idle for more than `ttl` seconds."""
        now = time.time()
        stale = [
            sid for sid, ts in self._session_locks_meta.items()
            if now - ts > ttl and not self._session_locks[sid].locked()
        ]
        for sid in stale:
            del self._session_locks[sid]
            del self._session_locks_meta[sid]
        if stale:
            log.debug("GC'd %d idle session lock(s)", len(stale))

    # ------------------------------------------------------------------ #
    # Task handling
    # ------------------------------------------------------------------ #
    async def _handle_task(self, ws, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id") or msg.get("task_id")
        payload    = msg.get("payload") or msg.get("request", {})
        session_id = payload.get("session_id") or request_id
        mode       = payload.get("mode", "new")

        # ── BETA feature 1: auto_continue promotion ──────────────────────
        prior = self.sessions.get(session_id)
        if prior and mode == "new" and payload.get("auto_continue"):
            log.info(
                "auto_continue: promoting session=%s from new → continue",
                session_id[:8],
            )
            mode = "continue"

        # ── Per-session lock for CONTINUE (anti-collision) ────────────────
        if session_id and mode == "continue":
            lock = await self._get_session_lock(session_id)
            async with lock:
                result, slot_index = await self._execute_task(
                    payload, session_id, mode, request_id
                )
        else:
            result, slot_index = await self._execute_task(
                payload, session_id, mode, request_id
            )

        # ── Persist session state ─────────────────────────────────────────
        if result.get("ok"):
            conv_url = result.get("conversation_url")
            account  = result.get("account")
            s = self.sessions.get_or_create(session_id, account=account)
            if conv_url:
                s.conversation_url = conv_url
            if account and not s.account:
                s.account = account
            s.touch()
            self.sessions.update(s)
            self.sessions.bump_turn(session_id)
            # BETA feature 2: pin slot index for next CONTINUE call.
            if slot_index is not None:
                self.sessions.upsert_slot(session_id, slot_index)
                self._session_slot[session_id] = slot_index
            log.debug(
                "Session %s: account=%s url=%s turns=%d slot=%s",
                session_id[:8], s.account,
                (s.conversation_url or "")[:60], s.turn_count, slot_index,
            )

        reply_type = "result" if result.get("ok") else "error"
        await ws.send(json.dumps({
            "type":       reply_type,
            "request_id": request_id,
            "task_id":    request_id,
            "worker_id":  self.worker_id,
            "session_id": session_id,
            "result":     result,
        }))

    async def _execute_task(
        self,
        payload: dict[str, Any],
        session_id: str,
        mode: str,
        request_id: str,
    ) -> tuple[dict, Optional[int]]:
        """
        Dispatch the task and return (result_dict, slot_index_used).

        Routing order:
          1. CONTINUE + tool_messages  → pool.run_task_with_tool_result()  (Turn 2)
          2. CONTINUE                  → pool.run_task() via pinned slot    (Turn N)
          3. NEW                       → pool.run_task() via any idle slot  (Turn 1)
        """
        assert self.pool is not None

        prompt     = payload.get("prompt", "")
        tool_msgs  = payload.get("tool_messages")
        attachments = payload.get("attachments")
        send_kwargs = {
            "model_tab":  payload.get("model_tab", "instant"),
            "deep_think": bool(payload.get("deep_think", False)),
            "web_search": bool(payload.get("web_search", False)),
        }

        # Resolve CONTINUE context from session store.
        conversation_url: Optional[str] = None
        preferred_account: Optional[str] = payload.get("preferred_account")
        pinned_slot_idx: Optional[int] = None

        if mode == "continue":
            existing = self.sessions.get(session_id)
            if existing:
                conversation_url  = existing.conversation_url
                if existing.account and not preferred_account:
                    preferred_account = existing.account
                pinned_slot_idx = self._session_slot.get(session_id)
                log.info(
                    "CONTINUE session=%s account=%s slot=%s url=%s",
                    session_id[:8], preferred_account, pinned_slot_idx,
                    (conversation_url or "")[:60],
                )
            else:
                log.warning(
                    "CONTINUE session=%s not found/expired — falling back to NEW",
                    session_id[:8],
                )
                mode = "new"

        # ── BETA feature 2: slot-pinned acquisition ───────────────────────
        # For CONTINUE, try to reuse the exact slot that last served this
        # session so the persistent-profile browser page is already warm.
        # pool.acquire_pinned() falls back to any idle slot automatically.
        try:
            if mode == "continue" and pinned_slot_idx is not None:
                slot = await self.pool.acquire_pinned(
                    slot_index=pinned_slot_idx, timeout=180
                )
            else:
                slot = await self.pool.acquire(timeout=180)

            slot_index_used = slot.index

            try:
                # Navigate to conversation URL if needed.
                if conversation_url and slot.scraper and slot.scraper.page:
                    current = slot.scraper.page.url or ""
                    already_there = (
                        conversation_url in current or current in conversation_url
                    )
                    if already_there:
                        log.info(
                            "BETA: skip goto() — slot %d already at URL", slot.index
                        )
                        slot.scraper._conversation_started = True
                    else:
                        log.info(
                            "BETA: navigating slot %d to %s",
                            slot.index, conversation_url,
                        )
                        try:
                            await slot.scraper.page.goto(
                                conversation_url,
                                wait_until="domcontentloaded",
                                timeout=30_000,
                            )
                            import asyncio as _a; await _a.sleep(2)
                            slot.scraper._conversation_started = True
                        except Exception as nav_exc:
                            log.warning("Nav to conv URL failed: %s", nav_exc)

                # ── BETA feature 3: Tool Turn 2 ───────────────────────────
                if mode == "continue" and tool_msgs:
                    messages   = payload.get("messages", [])
                    last_tool  = max(
                        (i for i, m in enumerate(messages) if m.get("role") == "tool"),
                        default=-1,
                    )
                    after_tools = messages[last_tool + 1:] if last_tool >= 0 else []
                    next_user   = next(
                        (m["content"] for m in after_tools
                         if m.get("role") == "user" and m.get("content")),
                        None,
                    )
                    log.info(
                        "BETA Turn 2: %d tool result(s), next_user=%s",
                        len(tool_msgs),
                        repr(next_user[:40]) if next_user else "None",
                    )
                    result = await slot.scraper.scrape_with_tool_result(
                        tool_messages=tool_msgs,
                        next_user_msg=next_user,
                    )
                else:
                    result = await slot.scraper.scrape(
                        prompt,
                        mode=mode,
                        attachments=attachments,
                        **send_kwargs,
                    )

                # Attach current URL so the caller can persist it.
                if result.get("ok") and slot.scraper and slot.scraper.page:
                    result.setdefault("conversation_url", slot.scraper.page.url)

            finally:
                # CONTINUE: keep page alive. NEW: reset to idle normally.
                await self.pool.release(slot, reset=(mode != "continue"))

            return result, slot_index_used

        except Exception as exc:
            log.error("Task %s failed: %s", request_id, exc, exc_info=True)
            return {"ok": False, "error": str(exc)}, None

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    async def _shutdown(self) -> None:
        self._stop.set()
        for task in (self._keepalive_task, self._cleanup_task):
            if task:
                task.cancel()
        if self._ws:
            await self._ws.close()
        sys.exit(0)


# =========================================================================== #
# CLI
# =========================================================================== #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PAF-ModelDeepSeek local worker (BETA)")
    p.add_argument("--vps", required=True,
                   help="VPS WebSocket URL (e.g. ws://HOST:8000/ws/worker)")
    p.add_argument("--workers", type=int, default=1,
                   help="Number of concurrent browser slots (default 1)")
    p.add_argument("--token", required=True,
                   help="Shared secret token for VPS authentication")
    p.add_argument("--no-headless", action="store_true",
                   help="Run browsers with visible UI")
    p.add_argument(
        "--session-ttl",
        type=int, default=_SESSION_TTL,
        help=f"Session TTL in seconds (default {_SESSION_TTL})",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    worker = LocalWorkerBeta(
        vps_url=args.vps,
        token=args.token,
        num_workers=args.workers,
        headless=not args.no_headless,
        session_ttl=args.session_ttl,
    )
    try:
        await worker.start()
        return 0
    except KeyboardInterrupt:
        log.info("Interrupted")
        return 130
    except Exception as exc:
        log.error("Fatal: %s", exc, exc_info=True)
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
