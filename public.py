#!/usr/bin/env python3
"""
public.py — Local worker.

Runs on a machine that is logged into DeepSeek (via cookies) and owns a
pre-warmed BrowserPool. Connects to the VPS over WebSocket, receives tasks,
runs them through the pool, and streams results back. Auto-reconnects.

Usage
-----
  python public.py --vps ws://VPS_IP:8000/ws/worker \\
      --workers 2 --token MY_SHARED_SECRET
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from browser_pool import BrowserPool
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.worker")


class LocalWorker:
    def __init__(
        self, vps_url: str, token: str, num_workers: int, headless: bool = True
    ) -> None:
        self.vps_url = vps_url
        self.token = token
        self.num_workers = num_workers
        self.headless = headless
        self.worker_id = f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.pool: BrowserPool | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        self.pool = BrowserPool(num_slots=self.num_workers, headless=self.headless)
        await self.pool.start()
        log.info("Worker %s pool ready: %s",
                 self.worker_id, self.pool.status_summary())
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
                log.warning("Connection lost (%s). Reconnecting in %ds...",
                            exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as exc:  # noqa: BLE001
                log.error("Unexpected worker error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _serve(self) -> None:
        log.info("Connecting to VPS: %s", self.vps_url)
        async with websockets.connect(self.vps_url, max_size=None) as ws:
            # Register
            await ws.send(json.dumps({
                "type": "register",
                "token": self.token,
                "worker_id": self.worker_id,
                "max_concurrent": self.num_workers,
                "accounts": self.pool.list_accounts() if self.pool else [],
            }))
            ack = json.loads(await ws.recv())
            if ack.get("type") != "registered":
                raise RuntimeError(f"Registration rejected: {ack}")
            log.info("Registered with VPS as %s", self.worker_id)

            await self._message_loop(ws)

    async def _message_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "task":
                # Run each task concurrently; the pool bounds real parallelism.
                asyncio.create_task(self._handle_task(ws, msg))
            elif mtype == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif mtype == "shutdown":
                log.info("Received shutdown from VPS")
                self._stop.set()
                break

    async def _handle_task(self, ws, msg: dict[str, Any]) -> None:
        request_id = msg.get("request_id")
        payload = msg.get("payload", {})
        prompt = payload.get("prompt", "")
        mode = payload.get("mode", "new")
        send_kwargs = {
            "model_tab": payload.get("model_tab", "instant"),
            "deep_think": bool(payload.get("deep_think", False)),
            "web_search": bool(payload.get("web_search", False)),
        }
        attachments = payload.get("attachments")
        log.info("Task %s: mode=%s tab=%s", request_id, mode, send_kwargs["model_tab"])

        try:
            assert self.pool is not None
            result = await self.pool.run_task(
                prompt, mode=mode, attachments=attachments, **send_kwargs
            )
            await ws.send(json.dumps({
                "type": "result",
                "request_id": request_id,
                "worker_id": self.worker_id,
                "result": result,
            }))
        except Exception as exc:  # noqa: BLE001
            log.error("Task %s failed: %s", request_id, exc)
            await ws.send(json.dumps({
                "type": "result",
                "request_id": request_id,
                "worker_id": self.worker_id,
                "result": {"ok": False, "error": str(exc)},
            }))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PAF-ModelDeepSeek local worker")
    p.add_argument("--vps", required=True,
                   help="VPS WebSocket URL, e.g. ws://1.2.3.4:8000/ws/worker")
    p.add_argument("--workers", type=int, default=1,
                   help="Number of pre-warmed browsers (pool size)")
    p.add_argument("--token", required=True, help="Shared auth token")
    p.add_argument("--no-headless", action="store_true",
                   help="Run browsers visibly")
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    worker = LocalWorker(
        vps_url=args.vps,
        token=args.token,
        num_workers=args.workers,
        headless=not args.no_headless,
    )
    try:
        await worker.start()
        return 0
    except KeyboardInterrupt:
        log.warning("Worker interrupted")
        return 130


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
