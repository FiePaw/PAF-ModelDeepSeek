"""
browser_pool.py — Enhanced BrowserPool with preferred_account routing & conversation_url navigation.

Each slot owns a logged-in DeepSeekScraper so that incoming tasks never pay the
cold-start cost of launching Chromium + injecting cookies. Dead slots are
respawned (rotating to another account where possible).

NEW FEATURES:
  - preferred_account routing
  - conversation_url navigation (CONTINUE mode)
  - Per-account headless control
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
    """Manager for N pre-warmed browser slots with enhanced routing."""

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
        self.email = email
        self.password = password
        self._accounts: list[str] = accounts or self._discover_account_names()
        self.slots: list[BrowserSlot] = []
        self._idle_event = asyncio.Event()
        self._started = False
        self._account_headless: dict[str, bool] = {}  # Per-account headless state

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
        if self._started:
            log.warning("Pool already started")
            return
        self._started = True
        self.slots = [BrowserSlot(index=i) for i in range(self.num_slots)]
        tasks = [self._init_slot(slot) for slot in self.slots]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_idle_event()
        log.info("BrowserPool started: %d slots", self.num_slots)

    async def stop(self) -> None:
        if not self._started:
            return
        log.info("Stopping BrowserPool...")
        tasks = [self._close_slot(slot) for slot in self.slots]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.slots.clear()
        self._started = False

    async def _init_slot(
        self, slot: BrowserSlot, account: Optional[str] = None, headless: Optional[bool] = None
    ) -> None:
        """Initialize a slot with a logged-in browser."""
        if account is None:
            account = self._account_for_slot(slot.index)
        if headless is None:
            headless = self._account_headless.get(account, self.headless)

        try:
            await self._close_slot(slot)
            scraper = DeepSeekScraper(
                headless=headless,
                account=account,
                email=self.email,
                password=self.password,
            )
            await scraper.launch_browser(account)
            # Warm the page
            await scraper._ensure_loaded()
            slot.scraper = scraper
            slot.account = account
            slot.fail_count = 0
            slot.mark_idle()
            log.info("Slot %d ready (account=%s, headless=%s)", slot.index, account, headless)
        except Exception as exc:
            slot.fail_count += 1
            slot.mark_dead()
            log.error("Slot %d failed to init: %s", slot.index, exc)

    async def _close_slot(self, slot: BrowserSlot) -> None:
        """Close browser in slot."""
        if slot.scraper:
            try:
                await slot.scraper.close_browser()
            except Exception as exc:
                log.warning("Error closing slot %d: %s", slot.index, exc)
            slot.scraper = None

    # ------------------------------------------------------------------ #
    # Respawn
    # ------------------------------------------------------------------ #
    def _schedule_respawn(self, slot: BrowserSlot) -> None:
        asyncio.create_task(self._respawn_slot(slot.index))

    async def _respawn_slot(self, index: int, headless: Optional[bool] = None) -> None:
        if index >= len(self.slots):
            return
        slot = self.slots[index]
        log.warning("Respawning dead slot %d", slot.index)
        await self._close_slot(slot)
        await asyncio.sleep(ROTATION_CONFIG["browser_restart_delay"])
        # Rotate to different account if slot failed repeatedly
        account = None
        if slot.fail_count > ROTATION_CONFIG["max_browser_restarts"]:
            current_idx = (
                self._accounts.index(slot.account)
                if slot.account in self._accounts
                else -1
            )
            account = self._accounts[(current_idx + 1) % len(self._accounts)]
            log.info("Rotating slot %d from %s to %s", slot.index, slot.account, account)
        await self._init_slot(slot, account=account, headless=headless)
        self._refresh_idle_event()

    # ------------------------------------------------------------------ #
    # Slot acquisition
    # ------------------------------------------------------------------ #
    async def acquire(self, timeout: float = 120.0) -> BrowserSlot:
        """Acquire any idle slot."""
        return await self._acquire_with_account(timeout=timeout, preferred_account=None)

    async def _acquire_with_account(
        self, timeout: float = 120.0, preferred_account: Optional[str] = None
    ) -> BrowserSlot:
        """
        Acquire a slot, optionally filtering by account.
        
        Args:
            preferred_account: If specified, wait for slot with this account.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for slot in self.slots:
                if slot.status != SlotStatus.IDLE:
                    continue
                # Filter by account if specified
                if preferred_account and slot.account != preferred_account:
                    continue
                async with slot._lock:
                    if slot.status == SlotStatus.IDLE:
                        slot.mark_busy()
                        return slot
            # Wait for any slot to become idle
            self._idle_event.clear()
            try:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                await asyncio.wait_for(self._idle_event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue

        # Timeout
        account_msg = f" with account {preferred_account}" if preferred_account else ""
        raise TimeoutError(f"No idle slot{account_msg} within {timeout}s")

    def _refresh_idle_event(self) -> None:
        """Signal that at least one slot is idle."""
        if any(s.status == SlotStatus.IDLE for s in self.slots):
            self._idle_event.set()

    # ------------------------------------------------------------------ #
    # Enhanced run_task with routing & navigation
    # ------------------------------------------------------------------ #
    async def run_task(
        self,
        prompt: str,
        mode: str = "new",
        attachments: Optional[list] = None,
        acquire_timeout: float = 120.0,
        preferred_account: Optional[str] = None,
        conversation_url: Optional[str] = None,
        **send_kwargs,
    ) -> dict:
        """
        Run a single task (scrape). Acquires slot, runs scrape(), releases.
        
        NEW FEATURES:
          - preferred_account: wait for slot with this account
          - conversation_url: navigate to URL before scrape (CONTINUE mode)
        """
        slot = await self._acquire_with_account(
            timeout=acquire_timeout, preferred_account=preferred_account
        )
        try:
            # Navigate to conversation URL if provided (CONTINUE mode)
            if conversation_url and slot.scraper and slot.scraper.page:
                log.info("CONTINUE: navigating to %s", conversation_url)
                try:
                    await slot.scraper.page.goto(
                        conversation_url, wait_until="networkidle", timeout=10000
                    )
                    await asyncio.sleep(1)  # Let page settle
                except Exception as nav_exc:
                    log.warning("Failed to navigate to conversation URL: %s", nav_exc)

            result = await slot.scraper.scrape(
                prompt, mode=mode, attachments=attachments, **send_kwargs
            )
            return result
        except Exception as exc:
            log.error("run_task failed on slot %d: %s", slot.index, exc, exc_info=True)
            slot.mark_dead()
            self._schedule_respawn(slot)
            return {"ok": False, "error": str(exc)}
        finally:
            if slot.status == SlotStatus.BUSY:
                slot.mark_idle()
            self._refresh_idle_event()

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def status_summary(self) -> str:
        idle = sum(1 for s in self.slots if s.status == SlotStatus.IDLE)
        busy = sum(1 for s in self.slots if s.status == SlotStatus.BUSY)
        dead = sum(1 for s in self.slots if s.status == SlotStatus.DEAD)
        return f"{idle} idle, {busy} busy, {dead} dead"

    # ------------------------------------------------------------------ #
    # Per-account headless control
    # ------------------------------------------------------------------ #
    async def set_account_headless(self, account_name: str, headless: bool) -> None:
        """
        Set headless mode for a specific account.
        Restarts any slots using this account.
        """
        self._account_headless[account_name] = headless
        log.info("Set headless=%s for account %s", headless, account_name)
        
        # Restart slots with this account
        for slot in self.slots:
            if slot.account == account_name:
                slot.mark_dead()
                await self._respawn_slot(slot.index, headless=headless)
