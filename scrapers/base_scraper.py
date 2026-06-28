"""
scrapers/base_scraper.py — BaseAIChatScraper (abstract).

Generic Playwright chat-scraper foundation. Platform-agnostic: all site-specific
behaviour is delegated to subclasses via abstract methods.

AUTH MODEL (profile-first, password-fallback)
---------------------------------------------
* Accounts are defined in ONE file: cookies/auth.json (loaded by AuthStore).
* Each account maps to a PERSISTENT browser profile at profiles/<account>/.
* The first run for an account logs in with email + password; the profile then
  stores the session. Later runs reuse the profile, and only log in again if the
  login DOM reappears (handled by the subclass's ensure_authenticated()).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from config import (
    AUTH_CONFIG,
    BROWSER_CONFIG,
    CODE_OUTPUT_DIR,
    COOKIES_DIR,
    DEBUG_DIR,
    OUTPUT_CONFIG,
    OUTPUT_DIR,
    PERSISTENT_CONTEXT_CONFIG,
    PROFILES_DIR,
    ROTATION_CONFIG,
)
from scrapers.utils import (
    AuthStore,
    cookie_editor_json_to_playwright,
    dump_json,
    get_logger,
)

# Best-effort .env loading so DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD can live in a
# .env file at the project root.
try:  # pragma: no cover
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

log = get_logger("paf_deepseek.base")


class BaseAIChatScraper(ABC):
    """Abstract base for browser-automation chat scrapers."""

    # ------------------------------------------------------------------ #
    # Construction / lifecycle
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        headless: Optional[bool] = None,
        account: Optional[str] = None,
        profile_dir: Optional[str | Path] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.headless: bool = (
            BROWSER_CONFIG["headless"] if headless is None else headless
        )
        # Optional explicit credentials override (e.g. CLI --email/--password).
        self.email: Optional[str] = email
        self.password: Optional[str] = password
        self._authenticated: bool = False
        self._explicit_profile_dir: Optional[Path] = (
            Path(profile_dir) if profile_dir else None
        )

        # Playwright handles
        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Accounts come from the single auth.json file.
        self.auth = AuthStore(AUTH_CONFIG["auth_file"])
        self._accounts: list[str] = self._discover_accounts()

        # Current account name (key into auth.json + profile dir name).
        self.account: str = account or (self._accounts[0] if self._accounts
                                        else "account1")
        self._account_index: int = (
            self._accounts.index(self.account)
            if self.account in self._accounts else 0
        )

        self._browser_restarts: int = 0

    # ----- account discovery / profiles ------------------------------- #
    def _discover_accounts(self) -> list[str]:
        """All account names defined in cookies/auth.json."""
        return self.auth.account_names()

    def _profile_dir_for(self, account: Optional[str]) -> Path:
        """Per-account persistent-profile directory."""
        if self._explicit_profile_dir:
            return self._explicit_profile_dir
        return PROFILES_DIR / (account or PERSISTENT_CONTEXT_CONFIG["default_profile"])

    @staticmethod
    def _profile_seeded(profile_dir: Path) -> bool:
        """Whether a persistent profile has stored state from a prior session."""
        if not profile_dir.exists():
            return False
        return any(profile_dir.iterdir())

    def _current_account(self) -> str:
        return self.account

    # ----- credentials ------------------------------------------------ #
    def _resolve_credentials(
        self, email: Optional[str] = None, password: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Resolve (email, password) for the current account in order:
          1. explicit args / self.email,self.password (CLI override)
          2. cookies/auth.json entry for self.account
          3. DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD env vars (or .env)
        """
        import os

        email = email or self.email
        password = password or self.password

        if not email or not password:
            creds = self.auth.get(self.account)
            if creds:
                email = email or creds.get("email")
                password = password or creds.get("password")

        if not email:
            email = os.environ.get(AUTH_CONFIG["env_email"])
        if not password:
            password = os.environ.get(AUTH_CONFIG["env_password"])
        return email, password

    # ----- launch ----------------------------------------------------- #
    async def launch_browser(self, account: Optional[str] = None) -> Page:
        """
        Launch a persistent browser context bound to the account's profile.
        The profile carries the saved session across runs, so no cookie
        injection is required after the first login.
        """
        if account is not None:
            self.account = account
            if account in self._accounts:
                self._account_index = self._accounts.index(account)

        if self._pw is None:
            self._pw = await async_playwright().start()

        profile_dir = self._profile_dir_for(self.account)
        profile_dir.mkdir(parents=True, exist_ok=True)
        seeded = self._profile_seeded(profile_dir)
        log.info("Launching profile '%s' (seeded=%s, headless=%s)",
                 self.account, seeded, self.headless)

        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            slow_mo=BROWSER_CONFIG["slow_mo"],
            viewport=BROWSER_CONFIG["viewport"],
            user_agent=BROWSER_CONFIG["user_agent"],
            locale=BROWSER_CONFIG["locale"],
            timezone_id=BROWSER_CONFIG["timezone_id"],
            args=PERSISTENT_CONTEXT_CONFIG["launch_args"],
        )
        self.browser = None  # persistent context owns the browser
        await self._apply_stealth(self.context)

        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        # A fresh profile starts unauthenticated; a seeded one *might* be valid.
        self._authenticated = False
        return self.page

    async def _apply_stealth(self, context: BrowserContext) -> None:
        """Light anti-automation patches + optional localStorage seeding."""
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        items = self._local_storage_items()
        if items:
            kvs = json.dumps(items)
            await context.add_init_script(
                f"(function(){{const kv={kvs};"
                "for (const k in kv) {try{localStorage.setItem(k, kv[k]);}"
                "catch(e){}}}})();"
            )

    def _local_storage_items(self) -> dict[str, str]:
        """Override in subclass to inject SPA localStorage tokens. Default empty."""
        return {}

    async def close_browser(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None

    async def _is_page_crashed(self) -> bool:
        """Detect a crashed/closed page via Playwright state + error text."""
        if self.page is None:
            return True
        try:
            if self.page.is_closed():
                return True
            content = (await self.page.content()).lower()
            return any(
                phrase in content
                for phrase in ROTATION_CONFIG["page_crash_phrases"]
            )
        except Exception:
            return True

    async def restart_browser(self, account: Optional[str] = None) -> Page:
        """Close and relaunch the browser, honouring max_browser_restarts."""
        self._browser_restarts += 1
        if self._browser_restarts > ROTATION_CONFIG["max_browser_restarts"]:
            raise RuntimeError("Exceeded max_browser_restarts")
        log.warning("Restarting browser (attempt %d)", self._browser_restarts)
        await self.close_browser()
        await asyncio.sleep(ROTATION_CONFIG["browser_restart_delay"])
        return await self.launch_browser(account or self.account)

    # ------------------------------------------------------------------ #
    # Optional cookie helpers (legacy / debugging — NOT the main flow)
    # ------------------------------------------------------------------ #
    async def load_cookies(self, path: str | Path) -> None:
        """Inject a Cookie-Editor JSON export (optional manual seeding)."""
        path = Path(path)
        if not path.exists() or self.context is None:
            return
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        cookies = cookie_editor_json_to_playwright(raw)
        await self.context.add_cookies(cookies)
        log.info("Loaded %d cookies from %s", len(cookies), path.name)

    async def save_cookies(self, path: Optional[str | Path] = None) -> None:
        """Export current cookies (debug/backup) to cookies/<account>.cookies.json."""
        if self.context is None:
            return
        path = Path(path) if path else (COOKIES_DIR / f"{self.account}.cookies.json")
        cookies = await self.context.cookies()
        dump_json(cookies, path)
        log.info("Saved %d cookies to %s", len(cookies), path.name)

    # ------------------------------------------------------------------ #
    # Account rotation
    # ------------------------------------------------------------------ #
    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """
        Switch to the next account from auth.json (and its profile). Returns
        True if rotation happened, False if there is no other account.
        """
        if len(self._accounts) <= 1:
            log.warning("No alternate account to rotate to")
            return False

        self._account_index = (self._account_index + 1) % len(self._accounts)
        next_account = self._accounts[self._account_index]
        log.warning("Rotating to account: %s", next_account)
        self.account = next_account
        self.email = None      # force re-resolve from auth.json for new account
        self.password = None
        self._authenticated = False
        await asyncio.sleep(ROTATION_CONFIG["rotation_delay"])

        self._browser_restarts = 0
        if restart_first:
            await self.restart_browser(next_account)
        else:
            await self.close_browser()
            await self.launch_browser(next_account)
        return True

    # ------------------------------------------------------------------ #
    # Response handling
    # ------------------------------------------------------------------ #
    async def _count_response_elements(self) -> dict[str, int]:
        """
        Snapshot how many response elements each selector currently matches.

        Returns a dict ``{selector: count}`` so that wait_for_response /
        _read_latest_response can use a **per-selector** baseline.

        FIX: The old version returned a single int from the FIRST selector
        (``div.ds-markdown:last-of-type``). Because :last-of-type matches one
        element per parent wrapper, its count could EQUAL the total
        ``div.ds-markdown`` count. Using that inflated number as a universal
        skip_count meant ``count > skip_count`` was never True for any
        selector → 300 s timeout.

        By returning per-selector counts, each selector is compared with
        its own baseline. A new response increments the count of at least one
        selector, and the detection works regardless of DOM grouping.
        """
        baselines: dict[str, int] = {}
        if self.page is None:
            return baselines
        for sel in self._response_selectors():
            try:
                count = await self.page.locator(sel).count()
                baselines[sel] = count
            except Exception:
                baselines[sel] = 0
        log.debug("Response baselines: %s", baselines)
        return baselines

    async def wait_for_response(
        self,
        response_selectors: list[str],
        timeout: float,
        stability_secs: float,
        stability_polls: int,
        poll_interval: float,
        initial_response_count: dict[str, int] | int = 0,
    ) -> str:
        """
        Wait until the AI response stops changing. The response is considered
        complete when its text is non-empty AND unchanged for `stability_polls`
        consecutive polls. More robust than waiting for a loading indicator.

        Args:
            initial_response_count: Per-selector baseline snapshot taken before
                the prompt was sent.  Accepts either a ``dict[str, int]``
                (per-selector, preferred) or a plain ``int`` for backward
                compatibility (applied to every selector).
        """
        # Normalise legacy int → per-selector dict
        if isinstance(initial_response_count, int):
            baselines = {sel: initial_response_count for sel in response_selectors}
        else:
            baselines = initial_response_count

        deadline = time.monotonic() + timeout
        last_text = ""
        stable_count = 0

        while time.monotonic() < deadline:
            text = await self._read_latest_response(
                response_selectors,
                baselines=baselines,
            )
            if text and text == last_text:
                stable_count += 1
                if stable_count >= stability_polls:
                    return text
            else:
                stable_count = 0
                last_text = text
            await asyncio.sleep(poll_interval)

        log.warning("wait_for_response timed out after %.0fs", timeout)
        return last_text

    async def _read_latest_response(
        self,
        selectors: list[str],
        baselines: dict[str, int] | None = None,
    ) -> str:
        """
        Return the text of the latest assistant message, trying selectors in order.

        Args:
            baselines: Per-selector baseline counts (from
                _count_response_elements).  For each selector, only proceed
                when the current count exceeds that selector's own baseline.
                This prevents the inflated skip_count problem where a
                :last-of-type selector produced the same number as the total
                element count, making ``count > skip`` always False.
        """
        if self.page is None:
            return ""
        baselines = baselines or {}
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                count = await loc.count()
                # Use this selector's OWN baseline, not a global number.
                skip = baselines.get(sel, 0)
                if count > skip:
                    text = await loc.nth(count - 1).inner_text()
                    if text and text.strip():
                        return text.strip()
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------------ #
    # Extraction / saving
    # ------------------------------------------------------------------ #
    _CODE_FENCE_RE = re.compile(
        r"```(?P<lang>[\w+\-.]*)\n(?P<code>.*?)```", re.DOTALL
    )

    _EXT_BY_LANG = {
        "python": ".py", "py": ".py", "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts", "bash": ".sh", "sh": ".sh",
        "shell": ".sh", "json": ".json", "html": ".html", "css": ".css",
        "java": ".java", "c": ".c", "cpp": ".cpp", "c++": ".cpp",
        "go": ".go", "rust": ".rs", "rs": ".rs", "sql": ".sql",
        "yaml": ".yaml", "yml": ".yaml", "markdown": ".md", "md": ".md",
    }

    def detect_file_type(self, content: str) -> str:
        """Heuristic file extension guess from code-block content."""
        c = content.strip()
        if c.startswith(("{", "[")):
            return ".json"
        if c.startswith("<!DOCTYPE html") or c.startswith("<html"):
            return ".html"
        if "def " in c or ("import " in c and "from " in c):
            return ".py"
        if "function " in c or "const " in c or "=>" in c:
            return ".js"
        if c.startswith("#!"):
            return ".sh"
        return ".txt"

    def extract_code_blocks(self, content: str) -> list[dict[str, str]]:
        """Parse markdown code fences into [{lang, code, ext}]."""
        blocks: list[dict[str, str]] = []
        for m in self._CODE_FENCE_RE.finditer(content or ""):
            lang = (m.group("lang") or "").strip().lower()
            code = m.group("code")
            ext = self._EXT_BY_LANG.get(lang) or self.detect_file_type(code)
            blocks.append({"lang": lang, "code": code, "ext": ext})
        return blocks

    def save_to_json(self, data: Any, filename: Optional[str] = None) -> Path:
        if filename is None:
            ts = datetime.now().astimezone().strftime(
                OUTPUT_CONFIG["timestamp_format"]
            )
            filename = f"response_{ts}.json"
        return dump_json(data, OUTPUT_DIR / filename)

    def save_code_files(
        self, blocks: list[dict[str, str]], prefix: Optional[str] = None
    ) -> list[Path]:
        ts = datetime.now().astimezone().strftime(OUTPUT_CONFIG["timestamp_format"])
        prefix = prefix or f"code_{ts}"
        saved: list[Path] = []
        for i, b in enumerate(blocks):
            path = CODE_OUTPUT_DIR / f"{prefix}_{i}{b['ext']}"
            path.write_text(b["code"], encoding=OUTPUT_CONFIG["encoding"])
            saved.append(path)
        if saved:
            log.info("Saved %d code file(s) to %s", len(saved), CODE_OUTPUT_DIR)
        return saved

    # ------------------------------------------------------------------ #
    # Error resilience
    # ------------------------------------------------------------------ #
    async def take_debug_screenshot(self, reason: str = "error") -> Optional[Path]:
        if self.page is None:
            return None
        ts = datetime.now().astimezone().strftime(OUTPUT_CONFIG["timestamp_format"])
        safe = re.sub(r"[^\w\-]+", "_", reason)[:40]
        path = DEBUG_DIR / f"{ts}_{safe}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            log.info("Saved debug screenshot: %s", path.name)
            return path
        except Exception:
            return None

    @staticmethod
    def _repair_unescaped_quotes(raw: str) -> str:
        """Repair JSON where string values contain raw (unescaped) quotes."""
        if not raw:
            return raw
        try:
            json.loads(raw)
            return raw
        except Exception:
            pass
        return re.sub(r'(?<=[\w\s])"(?=[\w\s])', r'\\"', raw)

    @staticmethod
    def _repair_tool_calls_arguments(raw: str) -> str:
        """Repair the `arguments` field of OpenAI-style tool calls."""
        if not raw or '"arguments"' not in raw:
            return raw
        return re.sub(
            r'"arguments"\s*:\s*"?(\{.*?\})"?(?=\s*[},])',
            lambda m: '"arguments": ' + json.dumps(m.group(1)),
            raw,
            flags=re.DOTALL,
        )

    @abstractmethod
    def _validate_response(self, raw: str) -> tuple[bool, str]:
        """Validate response structure before parsing. Returns (ok, cleaned)."""
        ...

    # ------------------------------------------------------------------ #
    # Abstract platform hooks
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def send_prompt(self, prompt: str, mode: str = "new", **kwargs) -> str:
        """Send a prompt; return a handle used by wait_for_response."""
        ...

    @abstractmethod
    async def is_rate_limited(self) -> bool:
        ...

    @abstractmethod
    async def is_session_expired(self) -> bool:
        ...

    @abstractmethod
    def _extra_send_kwargs(self) -> dict:
        """Platform-specific default send kwargs (e.g. deep_think/web_search)."""
        ...

    async def ensure_authenticated(self) -> bool:
        """
        Ensure the current session is logged in. Default no-op (returns True).
        Subclasses that support email+password login override this to perform
        login when the session/login DOM is detected. Must be idempotent.
        """
        return True

    # ------------------------------------------------------------------ #
    # Orchestrator
    # ------------------------------------------------------------------ #
    async def scrape(
        self,
        prompt: str,
        mode: str = "new",
        attachments: Optional[list[str | Path]] = None,
        **send_kwargs,
    ) -> dict[str, Any]:
        """
        Main orchestrator:
          launch/reuse profile -> authenticate (login if needed) -> send_prompt
          -> wait_for_response -> validate/repair -> extract -> save.
        """
        from config import DEEPSEEK_CONFIG  # keep base import-light

        if self.page is None:
            await self.launch_browser(self.account)

        merged_kwargs = {**self._extra_send_kwargs(), **send_kwargs}
        if attachments:
            merged_kwargs["attachments"] = attachments

        retries = 0
        max_retries = ROTATION_CONFIG["max_retries_per_account"]
        last_error: Optional[str] = None

        while retries <= max_retries:
            try:
                # --- TIMING instrumentation -----------------------------------
                # Records how long each stage of a single process takes so the
                # slow part is measurable instead of guessed. Emitted once per
                # attempt at log level INFO as a single [TIMING] line.
                _t0 = time.monotonic()
                _t_auth = _t_send = _t_wait = _t0

                if await self._is_page_crashed():
                    await self.restart_browser(self.account)

                # Authenticate (logs in with email+password if the login DOM is
                # present / session expired). If it ultimately fails, rotate.
                if not await self.ensure_authenticated():
                    log.warning("Authentication failed; rotating account")
                    if not await self._rotate_account(restart_first=False):
                        raise RuntimeError("Login failed and no account to rotate")
                _t_auth = time.monotonic()

                # Send the prompt first (which navigates for NEW mode).
                # For NEW mode, send_prompt -> _ensure_page_ready -> _goto_new_chat()
                # navigates to a fresh page BEFORE we can snapshot the baseline.
                # Snapshotting BEFORE send_prompt would read the OLD page's response
                # count (e.g. 3 from a previous CONTINUE session), causing
                # wait_for_response to wait for count > 3 on a fresh page that
                # only ever has 1 response -> 300s timeout.
                # Fix: for NEW mode, baseline is always 0 (fresh page has no
                # prior responses). For CONTINUE mode, snapshot AFTER we know
                # the page is stable (send_prompt does not navigate in CONTINUE).
                if mode == "new":
                    initial_response_count = {}
                    log.debug("NEW mode: response baseline forced to {} (fresh page)")
                    await self.send_prompt(prompt, mode=mode, **merged_kwargs)
                else:
                    # CONTINUE: snapshot before send (page is not navigated).
                    initial_response_count = await self._count_response_elements()
                    log.debug(
                        "CONTINUE mode: response baseline before send: %s",
                        initial_response_count,
                    )
                    await self.send_prompt(prompt, mode=mode, **merged_kwargs)
                _t_send = time.monotonic()

                t = DEEPSEEK_CONFIG["timeouts"]
                text = await self.wait_for_response(
                    response_selectors=self._response_selectors(),
                    timeout=t["response_wait"],
                    stability_secs=t["stability_check"],
                    stability_polls=t["stability_polls"],
                    poll_interval=t["poll_interval"],
                    initial_response_count=initial_response_count,
                )
                _t_wait = time.monotonic()
                log.info(
                    "[TIMING] auth=%.2fs send=%.2fs wait=%.2fs total=%.2fs "
                    "(mode=%s)",
                    _t_auth - _t0,
                    _t_send - _t_auth,
                    _t_wait - _t_send,
                    _t_wait - _t0,
                    mode,
                )

                if await self.is_rate_limited():
                    log.warning("Rate limited; attempting recovery")
                    await self.take_debug_screenshot("rate_limited")
                    if self._browser_restarts < ROTATION_CONFIG["max_browser_restarts"]:
                        await self.restart_browser(self.account)
                    else:
                        await self._rotate_account(restart_first=False)
                    retries += 1
                    await asyncio.sleep(ROTATION_CONFIG["retry_delay"])
                    continue

                ok, cleaned = self._validate_response(text)
                if not ok:
                    cleaned = self._repair_unescaped_quotes(cleaned)
                    cleaned = self._repair_tool_calls_arguments(cleaned)

                return {
                    "ok": True,
                    "mode": mode,
                    "account": self.account,
                    "text": cleaned,
                    "code_blocks": self.extract_code_blocks(cleaned),
                    "timestamp": datetime.now().astimezone().isoformat(),
                }

            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                log.error("scrape() error (retry %d): %s", retries, exc)
                await self.take_debug_screenshot("scrape_error")
                retries += 1
                await asyncio.sleep(ROTATION_CONFIG["retry_delay"])

        return {
            "ok": False,
            "mode": mode,
            "account": self.account,
            "error": last_error or "unknown error",
            "timestamp": datetime.now().astimezone().isoformat(),
        }

    @abstractmethod
    def _response_selectors(self) -> list[str]:
        """Return the ordered list of selectors used to read AI responses."""
        ...

    # ------------------------------------------------------------------ #
    # Async context manager
    # ------------------------------------------------------------------ #
    async def __aenter__(self) -> "BaseAIChatScraper":
        await self.launch_browser(self.account)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close_browser()
