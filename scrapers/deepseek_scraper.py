"""
scrapers/deepseek_scraper.py — DeepSeekScraper(BaseAIChatScraper).

Concrete implementation for chat.deepseek.com.

IMPORTANT — DOM verification
----------------------------
DeepSeek is a minified React SPA. Every selector is pulled from config and
marked there with TODO-verify notes. The scraper is intentionally *defensive*:
controls (model tabs, DeepThink/Search toggles) are best-effort. When a control
is missing or disabled for the active tab, the scraper logs a warning and
continues rather than crashing — because the available Layer-2 toggles DIFFER
per Layer-1 tab and that matrix is not fully documented yet.
"""
from __future__ import annotations

import asyncio
import base64
import mimetypes
from pathlib import Path
from typing import Optional

import json
import os

from config import AUTH_CONFIG, DEEPSEEK_CONFIG, BROWSER_CONFIG, ROTATION_CONFIG
from scrapers.base_scraper import BaseAIChatScraper
from scrapers.utils import get_logger

log = get_logger("paf_deepseek.scraper")

_SEL = DEEPSEEK_CONFIG["selectors"]
_T = DEEPSEEK_CONFIG["timeouts"]


class DeepSeekScraper(BaseAIChatScraper):
    """Browser-automation scraper for chat.deepseek.com."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._active_model_tab: str = DEEPSEEK_CONFIG["default_model_tab"]

    # ------------------------------------------------------------------ #
    # Generic selector helpers
    # ------------------------------------------------------------------ #
    async def _find_first(self, selectors: list[str], timeout_ms: int = 4000):
        """Return the first locator (from a list) that resolves to >=1 element."""
        if self.page is None:
            return None
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                await loc.first.wait_for(state="attached", timeout=timeout_ms)
                if await loc.count():
                    return loc.first
            except Exception:
                continue
        return None

    async def _click_first(self, selectors: list[str], timeout_ms: int = 4000) -> bool:
        loc = await self._find_first(selectors, timeout_ms)
        if loc is None:
            return False
        try:
            await loc.click(timeout=timeout_ms)
            await asyncio.sleep(_T["between_actions"])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Click failed for %s: %s", selectors[0], exc)
            return False

    async def _is_active(self, loc) -> bool:
        """Heuristic: is a tab/toggle currently active? Checks class + aria."""
        try:
            cls = (await loc.get_attribute("class")) or ""
            aria = (await loc.get_attribute("aria-checked")) or ""
            pressed = (await loc.get_attribute("aria-pressed")) or ""
            hint = DEEPSEEK_CONFIG["selectors"].get("active_marker_class_hint", "active")
            return (
                hint in cls.lower()
                or aria.lower() == "true"
                or pressed.lower() == "true"
            )
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Layer 1 — model tabs (Instant / Expert / Vision)
    # ------------------------------------------------------------------ #
    async def _select_model_tab(self, model_tab: str) -> None:
        model_tab = (model_tab or DEEPSEEK_CONFIG["default_model_tab"]).lower()
        tab_selectors = _SEL["model_tab"].get(model_tab)
        if not tab_selectors:
            log.warning("Unknown model_tab '%s'; staying on current tab", model_tab)
            return

        loc = await self._find_first(tab_selectors, timeout_ms=3000)
        if loc is None:
            log.warning(
                "Model tab '%s' not found in DOM — UI may differ. "
                "Continuing without switching tabs. (TODO: verify selectors)",
                model_tab,
            )
            return

        if await self._is_active(loc):
            log.info("Model tab '%s' already active", model_tab)
        else:
            try:
                await loc.click(timeout=3000)
                await asyncio.sleep(_T["between_actions"])
                log.info("Selected model tab '%s'", model_tab)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not click model tab '%s': %s", model_tab, exc)
                return
        self._active_model_tab = model_tab

    # ------------------------------------------------------------------ #
    # Layer 2 — DeepThink / Search toggles (availability differs per tab!)
    # ------------------------------------------------------------------ #
    async def _set_toggle(self, name: str, selectors: list[str], desired: bool) -> None:
        """
        Defensively set a Layer-2 toggle. If the toggle is absent or disabled in
        the currently-active model tab, log a warning and continue (do NOT crash).
        """
        if not desired:
            return  # only act when caller wants it ON; default state is OFF

        loc = await self._find_first(selectors, timeout_ms=2500)
        if loc is None:
            log.warning(
                "'%s' toggle not available for tab '%s' — skipping "
                "(Layer-2 options differ per Layer-1 tab; TODO: verify matrix).",
                name, self._active_model_tab,
            )
            return

        # Respect disabled state.
        try:
            disabled = (await loc.get_attribute("aria-disabled")) or ""
            if disabled.lower() == "true" or not await loc.is_enabled():
                log.warning(
                    "'%s' toggle is disabled for tab '%s' — skipping.",
                    name, self._active_model_tab,
                )
                return
        except Exception:
            pass

        if await self._is_active(loc):
            log.info("'%s' already ON", name)
            return
        try:
            await loc.click(timeout=2500)
            await asyncio.sleep(_T["between_actions"])
            log.info("Enabled '%s' toggle", name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not toggle '%s': %s", name, exc)

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    async def _goto_new_chat(self) -> None:
        if self.page is None:
            return
        # Prefer the in-app New chat button (keeps SPA state warm); fall back to
        # a hard navigation.
        clicked = await self._click_first(_SEL["new_chat_button"], timeout_ms=2500)
        if not clicked:
            await self.page.goto(
                DEEPSEEK_CONFIG["new_chat_url"],
                wait_until="domcontentloaded",
                timeout=_T["page_load"] * 1000,
            )
        await asyncio.sleep(_T["between_actions"])

    async def _ensure_loaded(self) -> None:
        if self.page is None:
            await self.launch_browser(self.account)
        assert self.page is not None
        if DEEPSEEK_CONFIG["base_url"] not in (self.page.url or ""):
            await self.page.goto(
                DEEPSEEK_CONFIG["base_url"],
                wait_until="domcontentloaded",
                timeout=_T["page_load"] * 1000,
            )
        # Profile-first: reuse the saved session. If the login DOM appears
        # (fresh profile or expired session), ensure_authenticated() logs in.
        await self.ensure_authenticated()

    # ------------------------------------------------------------------ #
    # Authentication — profile-first, email + password fallback
    # ------------------------------------------------------------------ #
    async def ensure_authenticated(self) -> bool:
        """
        Idempotent auth check.

        Flow: if the saved profile session is still valid -> done. If the login
        DOM is showing (fresh profile or expired session) -> log in with the
        email + password from cookies/auth.json for the current account, which
        repopulates the persistent profile. Returns True on success.
        """
        if self.page is None:
            return False

        if not await self.is_session_expired():
            self._authenticated = True
            return True

        log.info("Login DOM detected for account '%s' -> logging in", self.account)
        ok = await self.login()
        self._authenticated = ok
        return ok

    async def login(
        self, email: Optional[str] = None, password: Optional[str] = None
    ) -> bool:
        """
        Perform email + password login on the DeepSeek sign-in page, using the
        credentials resolved for the current account (auth.json / CLI / env).
        On success the persistent profile stores the session for next time.

        If an anti-bot captcha/slider appears, headless login cannot complete —
        re-run once with --no-headless to solve it (the profile remembers it).
        """
        if self.page is None:
            await self.launch_browser(self.account)
        assert self.page is not None

        email, password = self._resolve_credentials(email, password)
        if not email or not password:
            log.error(
                "No credentials for account '%s'. Add it to cookies/auth.json, "
                "pass --email/--password, or set %s / %s env vars.",
                self.account, AUTH_CONFIG["env_email"], AUTH_CONFIG["env_password"],
            )
            return False

        log.info("Logging in to DeepSeek as %s", email)
        await self.page.goto(
            AUTH_CONFIG["login_url"],
            wait_until="domcontentloaded",
            timeout=_T["page_load"] * 1000,
        )
        await asyncio.sleep(_T["between_actions"])

        login_sel = _SEL["login"]
        email_loc = await self._find_first(login_sel["email_input"], timeout_ms=8000)
        pwd_loc = await self._find_first(login_sel["password_input"], timeout_ms=8000)
        if email_loc is None or pwd_loc is None:
            log.error(
                "Login form fields not found (TODO: verify login selectors). "
                "Maybe already logged in?"
            )
            # Maybe we are already in the app.
            return not await self.is_session_expired()

        try:
            await email_loc.click()
            await email_loc.fill(email)
            await pwd_loc.click()
            await pwd_loc.fill(password)
            await asyncio.sleep(_T["between_actions"])
        except Exception as exc:  # noqa: BLE001
            log.error("Failed filling login form: %s", exc)
            return False

        # Optional consent checkbox (best-effort; ignore if absent).
        try:
            cb = await self._find_first(login_sel["agree_checkbox"], timeout_ms=1200)
            if cb is not None and not await cb.is_checked():
                await cb.check(timeout=1500)
        except Exception:
            pass

        # Submit.
        clicked = await self._click_first(login_sel["login_button"], timeout_ms=5000)
        if not clicked:
            log.info("Login button not clickable; pressing Enter")
            await pwd_loc.press("Enter")

        return await self._wait_for_login_result()

    async def _wait_for_login_result(self) -> bool:
        """Poll for login success (chat UI) / failure (captcha or error)."""
        assert self.page is not None
        deadline = asyncio.get_event_loop().time() + AUTH_CONFIG["login_wait"]
        login_sel = _SEL["login"]

        while asyncio.get_event_loop().time() < deadline:
            # Captcha / slider -> cannot proceed headlessly.
            captcha = await self._find_first(login_sel["captcha"], timeout_ms=800)
            if captcha is not None:
                msg = (
                    "Captcha/slider detected during login. Re-run once with "
                    "--no-headless to solve it; the persistent profile will "
                    "remember the session afterwards."
                )
                if AUTH_CONFIG.get("fail_loud_on_captcha", True):
                    log.error(msg)
                    await self.take_debug_screenshot("login_captcha")
                    return False
                log.warning(msg)

            # Success: we left the sign-in page AND the chat input is present.
            on_login = AUTH_CONFIG["login_url"] in (self.page.url or "")
            if not on_login:
                chat = await self._find_first(_SEL["chat_input"], timeout_ms=1500)
                if chat is not None:
                    await asyncio.sleep(AUTH_CONFIG["post_login_settle"])
                    log.info("Login successful — session saved in profile '%s'",
                             self.account)
                    self._authenticated = True
                    # The persistent profile already stores the session. Also
                    # export a cookie backup for debugging (best-effort).
                    try:
                        await self.save_cookies()
                    except Exception:
                        pass
                    return True

            # Inline error (wrong password etc.)
            err = await self._find_first(login_sel["error_message"], timeout_ms=600)
            if err is not None:
                try:
                    txt = (await err.inner_text()).strip()
                except Exception:
                    txt = ""
                if txt:
                    log.error("Login error: %s", txt)
                    await self.take_debug_screenshot("login_error")
                    return False

            await asyncio.sleep(1.0)

        log.error("Login timed out after %ss", AUTH_CONFIG["login_wait"])
        await self.take_debug_screenshot("login_timeout")
        return False

    # ------------------------------------------------------------------ #
    # send_prompt
    # ------------------------------------------------------------------ #
    async def send_prompt(
        self,
        prompt: str,
        mode: str = "new",
        model_tab: str = "instant",
        deep_think: bool = False,
        web_search: bool = False,
        attachments: Optional[list[str | Path]] = None,
        **kwargs,
    ) -> str:
        """
        Send a prompt to DeepSeek.

        Order: (new -> open new chat) -> select model tab (Layer 1) ->
        re-check + set Layer-2 toggles -> attach files -> type prompt -> send.
        Returns a handle string (here, the active response selector) for
        wait_for_response.
        """
        await self._ensure_loaded()

        if mode == "new":
            await self._goto_new_chat()

        # Layer 1 first — this can change which Layer-2 controls exist.
        await self._select_model_tab(model_tab)

        # Layer 2 — re-checked AFTER tab selection, defensively.
        await self._set_toggle("DeepThink", _SEL["deep_think_toggle"], deep_think)
        await self._set_toggle("Search", _SEL["web_search_toggle"], web_search)

        # Attachments (image/doc) via clipboard paste.
        if attachments:
            for att in attachments:
                await self._attach_via_clipboard(att)

        # Type prompt.
        input_loc = await self._find_first(_SEL["chat_input"], timeout_ms=8000)
        if input_loc is None:
            raise RuntimeError(
                "Chat input not found (TODO: verify #chat-input selector)."
            )
        await input_loc.click()
        # Use type() with a small per-char delay to look human.
        await input_loc.fill("")
        await input_loc.type(prompt, delay=BROWSER_CONFIG.get("type_delay_ms", 15))
        await asyncio.sleep(_T["between_actions"])

        # Send: prefer clicking the send button; fall back to Enter.
        sent = await self._click_first(_SEL["send_button"], timeout_ms=4000)
        if not sent:
            log.info("Send button not clickable; pressing Enter as fallback")
            await input_loc.press("Enter")

        # Return the selector wait_for_response should poll.
        return _SEL["assistant_message"][0]

    # ------------------------------------------------------------------ #
    # Attachments — CDP clipboard inject + Ctrl+V (NOT <input type=file>)
    # ------------------------------------------------------------------ #
    async def _attach_via_clipboard(self, file_path: str | Path) -> None:
        """
        Inject an image into the clipboard via the page context and paste it
        into the chat box with Ctrl+V. Many modern chat UIs (DeepSeek included)
        accept paste events but hide the native <input type=file>.

        NOTE: DeepSeek image input is associated with the Vision tab. Verify
        supported types (config.attachments.supported_types) before relying on
        non-image uploads — PDF/doc support is unconfirmed.
        """
        p = Path(file_path)
        if not p.exists():
            log.warning("Attachment not found: %s", p)
            return

        mime, _ = mimetypes.guess_type(str(p))
        mime = mime or "application/octet-stream"
        supported = DEEPSEEK_CONFIG["attachments"]["supported_types"]
        if mime not in supported:
            log.warning(
                "Attachment type '%s' may be unsupported by DeepSeek "
                "(supported: %s). Attempting anyway.",
                mime, supported,
            )

        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        # Build a File from base64 in the page and dispatch a paste event with
        # the file in clipboardData. This avoids needing OS clipboard access.
        js = """
        async ([b64, mime, name]) => {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const file = new File([bytes], name, {type: mime});
            const dt = new DataTransfer();
            dt.items.add(file);
            const target = document.querySelector('textarea#chat-input')
                || document.querySelector('div[contenteditable="true"]')
                || document.body;
            const evt = new ClipboardEvent('paste', {
                bubbles: true, cancelable: true, clipboardData: dt
            });
            target.dispatchEvent(evt);
            return true;
        }
        """
        try:
            await self.page.evaluate(js, [b64, mime, p.name])
            await asyncio.sleep(_T["between_actions"] * 2)
            log.info("Pasted attachment via clipboard: %s", p.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Clipboard paste failed for %s: %s", p.name, exc)

    # ------------------------------------------------------------------ #
    # State checks
    # ------------------------------------------------------------------ #
    async def is_rate_limited(self) -> bool:
        if self.page is None:
            return False
        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return any(
            phrase in body for phrase in ROTATION_CONFIG["rate_limit_phrases"]
        )

    async def is_session_expired(self) -> bool:
        """Fast check: are we on / showing the login DOM? (no long waits)."""
        if self.page is None:
            return False
        # 1) redirected to the sign-in URL (instant).
        if DEEPSEEK_CONFIG["login_url"] in (self.page.url or ""):
            return True
        # 2) login form present — use query_selector for an INSTANT check
        #    (no per-selector waiting, so this is cheap to call every request).
        for sel in _SEL["login_form"]:
            try:
                if await self.page.query_selector(sel) is not None:
                    return True
            except Exception:
                continue
        # 3) the chat input is present -> definitely authenticated.
        for sel in _SEL["chat_input"]:
            try:
                if await self.page.query_selector(sel) is not None:
                    return False
            except Exception:
                continue
        # 4) fall back to phrase match in the page body.
        try:
            body = (await self.page.inner_text("body")).lower()
        except Exception:
            return False
        return any(
            phrase in body
            for phrase in ROTATION_CONFIG["session_expired_phrases"]
        )

    # ------------------------------------------------------------------ #
    # localStorage seeding (optional SPA tokens)
    # ------------------------------------------------------------------ #
    def _local_storage_items(self) -> dict[str, str]:
        """
        Optional localStorage seeding. The persistent profile normally captures
        everything after the first login, so this is rarely needed. If you find
        auth-adjacent localStorage keys (DevTools -> Application -> Local Storage
        on chat.deepseek.com), drop them in cookies/<account>.localstorage.json
        and they'll be injected before first page load.
        TODO: verify which localStorage keys (if any) DeepSeek requires.
        """
        from config import COOKIES_DIR
        sidecar = COOKIES_DIR / f"{self.account}.localstorage.json"
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                return {str(k): str(v) for k, v in data.items()}
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed reading localStorage sidecar: %s", exc)
        return {}

    # ------------------------------------------------------------------ #
    # Validation / hooks
    # ------------------------------------------------------------------ #
    def _validate_response(self, raw: str) -> tuple[bool, str]:
        return self._validate_deepseek_response(raw)

    def _validate_deepseek_response(self, raw: str) -> tuple[bool, str]:
        """
        Validate a DeepSeek response before parsing. DeepSeek returns rendered
        markdown text (not a JSON envelope), so validation is lenient: non-empty
        text that is not obviously an error/limit notice is considered OK.
        """
        if not raw or not raw.strip():
            return False, ""
        low = raw.lower()
        for phrase in ROTATION_CONFIG["rate_limit_phrases"]:
            if phrase in low:
                return False, raw
        return True, raw.strip()

    def _extra_send_kwargs(self) -> dict:
        return {
            "model_tab": DEEPSEEK_CONFIG["default_model_tab"],
            "deep_think": DEEPSEEK_CONFIG["deep_think_default"],
            "web_search": DEEPSEEK_CONFIG["web_search_default"],
        }

    def _response_selectors(self) -> list[str]:
        # Prefer the explicit assistant message, fall back to the container.
        return _SEL["assistant_message"] + _SEL["response_container"]
