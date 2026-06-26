#!/usr/bin/env python3
"""
public.py — Enhanced Local Worker with Session Persistence & Interactive CLI.

Runs on a machine that is logged into DeepSeek (via cookies) and owns a
pre-warmed BrowserPool. Connects to the VPS over WebSocket, receives tasks,
runs them through the pool, and streams results back. Auto-reconnects.

NEW FEATURES:
  1. Session persistence (disk) via SessionStore
  2. CONTINUE: navigate to conversation URL
  3. Per-session lock (anti-collision CONTINUE)
  4. preferred_account routing with fallback
  5. Keepalive ping to VPS (anti-disconnect)
  6. update_accounts to VPS when pool ready
  7. Console interactive loop (CLI commands)
  8. add_account runtime (no restart)
  9. showheadless (toggle browser visibility per account)
  10. Error forwarding + HTTP status mapping
  11. Stream: true rejection (unsupported)
  12. Attachment via Attachment class (base64)
  13. Auto-reconnect to VPS (enhanced backoff)

Usage
-----
  python public.py --vps ws://VPS_IP:8000/ws/worker \
      --workers 2 --token MY_SHARED_SECRET

Commands (interactive REPL):
  list accounts     - Show all accounts
  add account NAME  - Add account runtime (auto-login)
  status            - Show pool status
  showheadless ACC  - Toggle headless for account
  quit              - Graceful shutdown
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool import BrowserPool
from config import DATA_SESSION_DIR
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.worker")


# =========================================================================== #
# SessionStore (Disk Persistence)
# =========================================================================== #
class SessionStore:
    """Persistent session storage for continue mode."""

    def __init__(self, storage_dir: Path = DATA_SESSION_DIR) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.storage_dir / f"{session_id}.json"

    def save(self, session_id: str, data: dict) -> None:
        try:
            self._path(session_id).write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Failed to save session %s: %s", session_id, e)

    def load(self, session_id: str) -> Optional[dict]:
        try:
            p = self._path(session_id)
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:
            log.warning("Failed to load session %s: %s", session_id, e)
        return None

    def delete(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink(missing_ok=True)
        except Exception as e:
            log.warning("Failed to delete session %s: %s", session_id, e)


# =========================================================================== #
# Enhanced LocalWorker
# =========================================================================== #
class LocalWorker:
    def __init__(
        self,
        vps_url: str,
        token: str,
        num_workers: int,
        headless: bool = True,
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id: Optional[str] = None  # Assigned by VPS
        self.pool: Optional[BrowserPool] = None
        self._stop = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session_store = SessionStore()
        self._session_locks: dict[str, asyncio.Lock] = {}  # Per-session locks
        self._keepalive_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # Main event loop reference

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        # Save reference to main event loop for CLI thread
        self._loop = asyncio.get_running_loop()
        
        self.pool = BrowserPool(num_slots=self.num_workers, headless=self.headless)
        await self.pool.start()
        log.info("Worker pool ready: %s", self.pool.status_summary())

        # Start interactive CLI in background thread
        threading.Thread(target=self._run_cli_loop, daemon=True).start()

        try:
            await self._connect_loop()
        finally:
            if self.pool:
                await self.pool.stop()

    async def _connect_loop(self) -> None:
        """Connect + auto-reconnect with backoff."""
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._serve()
                backoff = 1
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.warning("Connection lost (%s). Reconnecting in %ds...", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as exc:
                log.error("Unexpected worker error: %s", exc, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _serve(self) -> None:
        log.info("Connecting to VPS: %s", self.vps_url)
        async with websockets.connect(self.vps_url, max_size=None) as ws:
            self._ws = ws
            # Register (VPS will assign worker_id)
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "token": self.token,
                        "hostname": socket.gethostname(),
                        "max_concurrent": self.num_workers,
                        "accounts": self.pool.list_accounts() if self.pool else [],
                    }
                )
            )
            ack = json.loads(await ws.recv())
            if ack.get("type") != "registered":
                raise RuntimeError(f"Registration rejected: {ack}")
            self.worker_id = ack.get("worker_id")
            log.info("Registered with VPS as %s", self.worker_id)

            # Start keepalive ping
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            await self._message_loop(ws)

    async def _message_loop(self, ws) -> None:
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

    async def _keepalive_loop(self) -> None:
        """Send periodic ping to VPS to prevent idle timeout."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(30)
                if self._ws:
                    await self._ws.send(
                        json.dumps({"type": "ping", "worker_id": self.worker_id})
                    )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Task Handling
    # ------------------------------------------------------------------ #
    async def _handle_task(self, ws, msg: dict) -> None:
        task_id = msg.get("task_id")
        request = msg.get("request", {})

        try:
            # Feature 11: Reject stream: true
            if request.get("stream"):
                await self._send_error(
                    ws,
                    task_id,
                    "Streaming is not supported by this worker",
                    status=400,
                )
                return

            # Feature 3: Per-session lock
            session_id = request.get("session_id")
            mode = request.get("mode", "new")
            if session_id and mode == "continue":
                lock = self._session_locks.setdefault(session_id, asyncio.Lock())
                async with lock:
                    result = await self._execute_task(request)
            else:
                result = await self._execute_task(request)

            # Feature 10: Error forwarding with HTTP status
            if not result.get("ok"):
                error_msg = result.get("error", "Unknown error")
                status = self._map_error_to_status(error_msg)
                await self._send_error(ws, task_id, error_msg, status=status)
                return

            # Feature 6: Save session after successful task
            if session_id and result.get("conversation_url"):
                self.session_store.save(
                    session_id,
                    {
                        "conversation_url": result["conversation_url"],
                        "account": result.get("account"),
                        "updated_at": datetime.now().isoformat(),
                    },
                )

            await ws.send(
                json.dumps(
                    {
                        "type": "result",
                        "task_id": task_id,
                        "result": result,
                        "worker_id": self.worker_id,
                    }
                )
            )
        except Exception as exc:
            log.error("Task %s failed: %s", task_id, exc, exc_info=True)
            await self._send_error(ws, task_id, str(exc), status=500)

    async def _execute_task(self, request: dict) -> dict:
        """Execute task with enhanced features."""
        if not self.pool:
            return {"ok": False, "error": "Pool not initialized"}

        # Extract request fields
        prompt = request.get("prompt", "")
        mode = request.get("mode", "new")
        session_id = request.get("session_id")
        model_tab = request.get("model_tab", "instant")
        deep_think = request.get("deep_think", False)
        web_search = request.get("web_search", False)
        preferred_account = request.get("preferred_account")

        # Feature 12: Attachment support via base64
        attachments = None
        if request.get("attachments"):
            attachments = []
            for att in request["attachments"]:
                # Convert to path (base64 decoded to temp file by scraper)
                attachments.append(
                    {
                        "filename": att.get("filename", "file"),
                        "data": att.get("data", ""),
                        "mime_type": att.get("mime_type", "application/octet-stream"),
                    }
                )

        # Feature 2: CONTINUE mode - navigate to conversation URL
        conversation_url = None
        if mode == "continue" and session_id:
            session_data = self.session_store.load(session_id)
            if session_data:
                conversation_url = session_data.get("conversation_url")
                log.info("CONTINUE mode: navigating to %s", conversation_url)

        # Feature 4: preferred_account routing with fallback
        if preferred_account:
            # Check if preferred account is available
            available = self.pool.list_accounts()
            if preferred_account not in available:
                log.warning(
                    "Preferred account %s not available, using fallback", preferred_account
                )
                preferred_account = None

        # Run task through pool
        try:
            result = await self.pool.run_task(
                prompt=prompt,
                mode=mode,
                model_tab=model_tab,
                deep_think=deep_think,
                web_search=web_search,
                attachments=attachments,
                preferred_account=preferred_account,
                conversation_url=conversation_url,
            )
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def _send_error(
        self, ws, task_id: str, error: str, status: int = 500
    ) -> None:
        """Feature 13: Send error message to VPS (triggers reject_future)."""
        await ws.send(
            json.dumps(
                {
                    "type": "error",
                    "task_id": task_id,
                    "error": error,
                    "status": status,
                    "worker_id": self.worker_id,
                }
            )
        )

    def _map_error_to_status(self, error: str) -> int:
        """Feature 10: Map error messages to HTTP status codes."""
        error_lower = error.lower()
        if any(
            phrase in error_lower
            for phrase in ["rate limit", "too many requests", "请求过于频繁"]
        ):
            return 429
        if any(phrase in error_lower for phrase in ["timeout", "timed out"]):
            return 504
        if any(phrase in error_lower for phrase in ["not found", "404"]):
            return 404
        if any(
            phrase in error_lower
            for phrase in ["unauthorized", "authentication", "login"]
        ):
            return 401
        return 500

    # ------------------------------------------------------------------ #
    # Feature 7: Interactive CLI (REPL)
    # ------------------------------------------------------------------ #
    def _run_cli_loop(self) -> None:
        """Run interactive CLI in background thread."""
        print("\n" + "=" * 60)
        print("🎮 Interactive Worker Console")
        print("=" * 60)
        print("Commands:")
        print("  list accounts      - Show all accounts")
        print("  add account NAME   - Add account runtime (auto-login)")
        print("  status             - Show pool status")
        print("  showheadless ACC   - Toggle headless for account")
        print("  quit               - Graceful shutdown")
        print("=" * 60 + "\n")

        while not self._stop.is_set():
            try:
                cmd = input("worker> ").strip()
                if not cmd:
                    continue
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._handle_command(cmd), self._loop
                    )
            except (EOFError, KeyboardInterrupt):
                print("\nGraceful shutdown...")
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._shutdown(), self._loop
                    )
                break
            except Exception as e:
                print(f"Error: {e}")

    async def _handle_command(self, cmd: str) -> None:
        """Handle CLI commands."""
        parts = cmd.split()
        if not parts:
            return

        command = parts[0].lower()

        # list accounts
        if command == "list" and len(parts) == 2 and parts[1] == "accounts":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            accounts = self.pool.list_accounts()
            print(f"\n📋 Accounts ({len(accounts)}):")
            for i, acc in enumerate(accounts, 1):
                print(f"  {i}. {acc}")
            print()

        # add account NAME
        elif command == "add" and len(parts) == 3 and parts[1] == "account":
            account_name = parts[2]
            await self._add_account_runtime(account_name)

        # status
        elif command == "status":
            if not self.pool:
                print("❌ Pool not initialized")
                return
            print(f"\n📊 Pool Status: {self.pool.status_summary()}")
            print(f"   Worker ID: {self.worker_id}")
            print(f"   Connected: {self._ws is not None}")
            print()

        # showheadless ACC
        elif command == "showheadless" and len(parts) == 2:
            account_name = parts[1]
            await self._toggle_headless(account_name)

        # quit
        elif command == "quit":
            await self._shutdown()

        else:
            print(f"❌ Unknown command: {cmd}")
            print("   Type 'help' to see available commands")

    # ------------------------------------------------------------------ #
    # Feature 8: Add account runtime (auto-login)
    # ------------------------------------------------------------------ #
    async def _add_account_runtime(self, account_name: str) -> None:
        """Add account to pool and auto-login."""
        if not self.pool:
            print("❌ Pool not initialized")
            return

        try:
            print(f"➕ Adding account: {account_name}")
            self.pool.add_account(account_name)

            # Auto-login by spawning a slot for this account
            # The pool will handle login on first use
            print(f"✅ Account {account_name} added")

            # Feature 6: Send update_accounts to VPS
            await self._update_accounts_to_vps()

        except Exception as e:
            print(f"❌ Failed to add account: {e}")

    # ------------------------------------------------------------------ #
    # Feature 9: Toggle headless per account
    # ------------------------------------------------------------------ #
    async def _toggle_headless(self, account_name: str) -> None:
        """Toggle headless mode for a specific account."""
        if not self.pool:
            print("❌ Pool not initialized")
            return

        # Find slot with this account
        slot = None
        for s in self.pool.slots:
            if s.account == account_name:
                slot = s
                break

        if not slot:
            print(f"❌ Account {account_name} not found in active slots")
            return

        try:
            # Respawn slot with toggled headless
            current_headless = slot.scraper.headless if slot.scraper else True
            new_headless = not current_headless

            print(
                f"🔄 Toggling headless for {account_name}: {current_headless} → {new_headless}"
            )

            # Mark slot as dead and respawn
            slot.mark_dead()
            await self.pool._respawn_slot(slot.index, headless=new_headless)

            print(f"✅ Browser for {account_name} now visible={not new_headless}")

        except Exception as e:
            print(f"❌ Failed to toggle headless: {e}")

    # ------------------------------------------------------------------ #
    # Feature 6: Update accounts to VPS
    # ------------------------------------------------------------------ #
    async def _update_accounts_to_vps(self) -> None:
        """Send updated account list to VPS."""
        if not self._ws or not self.pool:
            return

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "update_accounts",
                        "worker_id": self.worker_id,
                        "accounts": self.pool.list_accounts(),
                    }
                )
            )
            log.info("Sent account update to VPS")
        except Exception as e:
            log.warning("Failed to send account update: %s", e)

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #
    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        print("\n🛑 Shutting down worker...")
        self._stop.set()
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._ws:
            await self._ws.close()
        sys.exit(0)


# =========================================================================== #
# Main
# =========================================================================== #
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="PAF-ModelDeepSeek enhanced local worker with interactive CLI"
    )
    p.add_argument(
        "--vps",
        required=True,
        help="VPS WebSocket URL (e.g., ws://VPS_IP:8000/ws/worker)",
    )
    p.add_argument(
        "--token", required=True, help="Shared secret token for authentication"
    )
    p.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent browser slots (default 1)",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browsers in headless mode (default True)",
    )
    p.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browsers with visible UI (overrides --headless)",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    headless = not args.no_headless if args.no_headless else args.headless

    worker = LocalWorker(
        vps_url=args.vps, token=args.token, num_workers=args.workers, headless=headless
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
