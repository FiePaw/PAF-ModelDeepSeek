"""
browser_pool.py — BrowserPool: a pool of pre-warmed DeepSeek browser slots.

Each slot owns a logged-in DeepSeekScraper so that incoming tasks never pay the
cold-start cost of launching Chromium + injecting cookies. Dead slots are
respawned (rotating to another account where possible).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from config import COOKIES_DIR, ROTATION_CONFIG
from scrapers.deepseek_scraper import DeepSeekScraper
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.pool")


class SlotStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    DEAD = "dead"


@dataclass
class BrowserSlot:
    index: int
    scraper: Optional[DeepSeekScraper] = None
    status: SlotStatus = SlotStatus.DEAD
    account: Optional[str] = None
    last_used: float = 0.0
    fail_count: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def mark_busy(self) -> None:
        self.status = SlotStatus.BUSY
        self.last_used = asyncio.get_event_loop().time()

    def mark_idle(self) -> None:
        self.status = SlotStatus.IDLE
        self.last_used = asyncio.get_event_loop().time()

    def mark_dead(self) -> None:
        self.status = SlotStatus.DEAD


class BrowserPool:
    """Manager for N pre-warmed browser slots."""

    def __init__(
        self,
        num_slots: int = 1,
        headless: bool = True,
        accounts: Optional[list[str]] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.num_slots = max(1, num_slots)
        self.headless = headless
        # Optional shared credentials override. Per-account credentials are
        # otherwise auto-resolved from cookies/auth.json (or DEEPSEEK_EMAIL /
        # DEEPSEEK_PASSWORD env vars).
        self.email = email
        self.password = password
        # Account names come from cookies/auth.json (via AuthStore).
        self._accounts: list[str] = accounts or self._discover_account_names()
        self.slots: list[BrowserSlot] = []
        self._idle_event = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    @staticmethod
    def _discover_account_names() -> list[str]:
        from scrapers.utils import AuthStore
        from config import AUTH_CONFIG
        names = AuthStore(AUTH_CONFIG["auth_file"]).account_names()
        return names or ["account1"]

    def _account_for_slot(self, index: int) -> Optional[str]:
        if not self._accounts:
            return None
        return self._accounts[index % len(self._accounts)]

    def list_accounts(self) -> list[str]:
        return list(self._accounts)

    def busy_accounts(self) -> list[str]:
        return [s.account for s in self.slots if s.status == SlotStatus.BUSY and s.account]

    def add_account(self, account_name: str) -> None:
        """Add an account to the pool dynamically (no full restart)."""
        if account_name not in self._accounts:
            self._accounts.append(account_name)
            log.info("Added account to pool: %s", account_name)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        log.info("Starting BrowserPool with %d slot(s)", self.num_slots)
        self.slots = [BrowserSlot(index=i) for i in range(self.num_slots)]
        await asyncio.gather(*(self._init_slot(s) for s in self.slots))
        self._started = True
        self._refresh_idle_event()
        log.info("BrowserPool ready: %s", self.status_summary())

    async def stop(self) -> None:
        log.info("Stopping BrowserPool")
        await asyncio.gather(
            *(self._close_slot(s) for s in self.slots),
            return_exceptions=True,
        )
        self._started = False

    async def _close_slot(self, slot: BrowserSlot) -> None:
        if slot.scraper:
            try:
                await slot.scraper.close_browser()
            except Exception:
                pass
        slot.scraper = None
        slot.mark_dead()

    async def _init_slot(
        self, slot: BrowserSlot, account: Optional[str] = None,
        headless: Optional[bool] = None,
    ) -> None:
        account = account or self._account_for_slot(slot.index)
        try:
            scraper = DeepSeekScraper(
                headless=self.headless if headless is None else headless,
                account=account,
                email=self.email,
                password=self.password,
            )
            await scraper.launch_browser(account)
            # Warm the page so the first task is instant.
            await scraper._ensure_loaded()
            slot.scraper = scraper
            slot.account = account
            slot.fail_count = 0
            slot.mark_idle()
            log.info("Slot %d ready (account=%s)", slot.index, account)
        except Exception as exc:  # noqa: BLE001
            slot.fail_count += 1
            slot.mark_dead()
            log.error("Slot %d failed to init: %s", slot.index, exc)

    # ------------------------------------------------------------------ #
    # Respawn
    # ------------------------------------------------------------------ #
    def _schedule_respawn(self, slot: BrowserSlot) -> None:
        asyncio.create_task(self._respawn_slot(slot))

    async def _respawn_slot(self, slot: BrowserSlot) -> None:
        log.warning("Respawning dead slot %d", slot.index)
        await self._close_slot(slot)
        await asyncio.sleep(ROTATION_CONFIG["browser_restart_delay"])
        # Rotate to a different account when respawning a repeatedly-dead slot.
        next_account = None
        if self._accounts:
            offset = slot.fail_count
            next_account = self._accounts[(slot.index + offset) % len(self._accounts)]
        await self._init_slot(slot, account=next_account)
        self._refresh_idle_event()

    # ------------------------------------------------------------------ #
    # Acquire / release
    # ------------------------------------------------------------------ #
    def _refresh_idle_event(self) -> None:
        if any(s.status == SlotStatus.IDLE for s in self.slots):
            self._idle_event.set()
        else:
            self._idle_event.clear()

    async def _wait_for_idle_slot(self, timeout: float) -> Optional[BrowserSlot]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for slot in self.slots:
                if slot.status == SlotStatus.IDLE:
                    return slot
            self._idle_event.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._idle_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        return None

    async def acquire(self, timeout: float = 120.0) -> BrowserSlot:
        """Acquire an IDLE slot, marking it BUSY. Waits up to `timeout`."""
        if not self._started:
            raise RuntimeError("Pool not started")
        slot = await self._wait_for_idle_slot(timeout)
        if slot is None:
            raise TimeoutError("No idle browser slot available")
        slot.mark_busy()
        self._refresh_idle_event()
        return slot

    async def release(self, slot: BrowserSlot, reset: bool = True) -> None:
        """Return a slot to the pool, optionally resetting its page first."""
        try:
            if slot.scraper and await slot.scraper._is_page_crashed():
                slot.mark_dead()
                self._schedule_respawn(slot)
                return
            if reset and slot.scraper:
                await self._reset_slot_page(slot)
            slot.mark_idle()
        except Exception as exc:  # noqa: BLE001
            log.warning("Error releasing slot %d: %s", slot.index, exc)
            slot.mark_dead()
            self._schedule_respawn(slot)
        finally:
            self._refresh_idle_event()

    async def _reset_slot_page(self, slot: BrowserSlot) -> None:
        """Reset page state (new chat) so the slot is clean for the next task."""
        if slot.scraper:
            try:
                await slot.scraper._goto_new_chat()
            except Exception:
                # Non-fatal; a respawn will fix a truly broken page.
                pass

    # ------------------------------------------------------------------ #
    # Convenience runner
    # ------------------------------------------------------------------ #
    async def run_task(
        self, prompt: str, mode: str = "new",
        attachments: Optional[list] = None, acquire_timeout: float = 120.0,
        **send_kwargs,
    ) -> dict:
        """Acquire a slot, run scrape(), release. Marks slot dead on failure."""
        slot = await self.acquire(timeout=acquire_timeout)
        try:
            result = await slot.scraper.scrape(
                prompt, mode=mode, attachments=attachments, **send_kwargs
            )
            return result
        except Exception as exc:  # noqa: BLE001
            log.error("run_task failed on slot %d: %s", slot.index, exc)
            slot.mark_dead()
            self._schedule_respawn(slot)
            return {"ok": False, "error": str(exc)}
        finally:
            if slot.status == SlotStatus.BUSY:
                await self.release(slot)

    # ------------------------------------------------------------------ #
    # Debug / observability
    # ------------------------------------------------------------------ #
    def status_summary(self) -> dict:
        counts = {s.value: 0 for s in SlotStatus}
        for slot in self.slots:
            counts[slot.status.value] += 1
        return {
            "slots": len(self.slots),
            "accounts": self._accounts,
            "status": counts,
            "detail": [
                {"index": s.index, "status": s.status.value, "account": s.account}
                for s in self.slots
            ],
        }

    async def restart_slot_no_headless(self, account_name: str) -> bool:
        """Restart the slot serving `account_name` in VISIBLE (non-headless)
        mode — useful for manual captcha solving or re-login."""
        for slot in self.slots:
            if slot.account == account_name:
                await self._close_slot(slot)
                await self._init_slot(slot, account=account_name, headless=False)
                self._refresh_idle_event()
                log.info("Restarted slot %d for %s in non-headless mode",
                         slot.index, account_name)
                return True
        log.warning("No slot found for account %s", account_name)
        return False

    async def stop_all_no_headless(self) -> None:
        """Relaunch every slot in visible mode (debugging)."""
        for slot in self.slots:
            await self._close_slot(slot)
            await self._init_slot(slot, account=slot.account, headless=False)
        self._refresh_idle_event()
