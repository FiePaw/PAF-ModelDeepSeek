"""
scrapers/deepseek_scraper.py — DeepSeekScraper(BaseAIChatScraper).

Concrete implementation for chat.deepseek.com.

IMPORTANT — DOM verification
----------------------------
DeepSeek is a minified React SPA. Every selector is pulled from config and
marked there with TODO-verify notes. The scraper is intentionally *defensive*:
controls (mode selector, DeepThink/Search tools) are best-effort. When a control
is missing or disabled for the active mode, the scraper logs a warning and
continues rather than crashing — because the available tools DIFFER per mode
(e.g. Search is only available on Instant mode).
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
        """
        Heuristic: is a mode pill / tool toggle currently active?

        Checks (in order):
          1. aria-checked / aria-pressed / aria-selected attributes
          2. Class fragment hint (e.g. "active" substring in class)
          3. Class-count comparison: DeepSeek's active pill gets an EXTRA
             minified class (e.g. _31a22b0) that inactive siblings lack.
             If the element and its sibling(s) can be compared, the one
             with more classes is considered active.
        """
        try:
            cls = (await loc.get_attribute("class")) or ""
            aria_checked  = (await loc.get_attribute("aria-checked"))  or ""
            aria_pressed  = (await loc.get_attribute("aria-pressed"))  or ""
            aria_selected = (await loc.get_attribute("aria-selected")) or ""
            hint = DEEPSEEK_CONFIG["selectors"].get("active_marker_class_hint", "active")

            # Check 1: ARIA state attributes
            if (
                aria_checked.lower()  == "true"
                or aria_pressed.lower()  == "true"
                or aria_selected.lower() == "true"
            ):
                return True

            # Check 2: Class fragment hint
            if hint and hint in cls.lower():
                return True

            return False
        except Exception:
            return False

    async def _is_active_by_class_count(self, loc) -> bool:
        """
        DeepSeek-specific heuristic: the active mode pill has MORE CSS classes
        than its inactive siblings (e.g. active pill has 3 classes, inactive
        has 2). Compare the target element's class count against its siblings.

        Returns True if the element has strictly more classes than at least one
        sibling. Returns False if comparison is impossible (no siblings, no
        class, or any exception).
        """
        try:
            cls = (await loc.get_attribute("class")) or ""
            my_count = len(cls.split())
            if my_count == 0:
                return False

            # Navigate to parent, then check sibling class counts.
            parent = loc.locator('..')
            if await parent.count() == 0:
                return False

            siblings = parent.first.locator('> *')
            sib_count = await siblings.count()
            if sib_count < 2:
                return False  # no siblings to compare

            min_sib_classes = my_count  # start with own count
            for i in range(sib_count):
                sib = siblings.nth(i)
                sib_cls = (await sib.get_attribute("class")) or ""
                sib_len = len(sib_cls.split())
                if sib_len > 0 and sib_len < min_sib_classes:
                    min_sib_classes = sib_len

            if my_count > min_sib_classes:
                log.debug(
                    "Active by class-count heuristic: %d classes vs sibling min %d",
                    my_count, min_sib_classes,
                )
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Layer 1 — Mode selector (Instant / Expert / Vision)
    # ------------------------------------------------------------------ #
    async def _select_model_tab(self, model_tab: str) -> None:
        model_tab = (model_tab or DEEPSEEK_CONFIG["default_model_tab"]).lower()
        tab_selectors = _SEL["model_tab"].get(model_tab)
        if not tab_selectors:
            log.warning("Unknown model_tab '%s'; staying on current tab", model_tab)
            return

        # --- Attempt 1: use configured selectors (fast path) -----------------
        loc = await self._find_first(tab_selectors, timeout_ms=3000)

        # --- Attempt 2: text-content scan fallback ---------------------------
        # DeepSeek mode pills are plain divs whose only stable attribute is
        # their visible text. The text label is a CHILD div inside the clickable
        # pill parent. We find the text label, then navigate UP to the parent
        # pill (the clickable element that carries the active class).
        if loc is None and self.page is not None:
            label = model_tab.capitalize()  # "Instant" | "Expert" | "Vision"
            try:
                candidates = self.page.get_by_text(label, exact=True)
                count = await candidates.count()
                if count > 0:
                    text_el = candidates.first
                    # Try to click the PARENT of the text label (the pill div),
                    # because the click handler is typically on the pill, not the
                    # text child. Use locator('..') to go up one level.
                    parent = text_el.locator('..')
                    parent_count = await parent.count()
                    if parent_count > 0:
                        loc = parent.first
                        log.debug(
                            "Mode '%s' found via get_by_text→parent fallback "
                            "(%d text match(es))",
                            model_tab, count,
                        )
                    else:
                        loc = text_el
                        log.debug(
                            "Mode '%s' found via get_by_text fallback (no parent) "
                            "(%d text match(es))",
                            model_tab, count,
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("get_by_text fallback failed for mode '%s': %s", model_tab, exc)

        if loc is None:
            log.warning(
                "Mode '%s' not found in DOM after selector + text-scan fallback. "
                "Continuing on current mode. "
                "(Hint: open DevTools on chat.deepseek.com and inspect the mode pills "
                "to update config.py selectors[model_tab])",
                model_tab,
            )
            return

        # Detect whether the located pill is already the active mode.
        # DeepSeek marks the active pill with an extra minified class (e.g.
        # _31a22b0). Since the class name changes between builds, we compare
        # class count: the active pill has MORE classes than inactive siblings.
        already_active = await self._is_active(loc)
        if not already_active:
            already_active = await self._is_active_by_class_count(loc)

        if already_active:
            log.info("Mode '%s' already active", model_tab)
        else:
            try:
                await loc.click(timeout=3000)
                await asyncio.sleep(_T["between_actions"])
                log.info("Selected mode '%s'", model_tab)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not select mode '%s': %s", model_tab, exc)
                return
        self._active_model_tab = model_tab

    # ------------------------------------------------------------------ #
    # Layer 2 — Tools: DeepThink / Search (availability differs per mode!)
    # ------------------------------------------------------------------ #

    # Internal mapping: tool name (lowercased) -> matrix key in config.
    _TOOL_MATRIX_KEY: dict[str, str] = {
        "deepthink": "deep_think",
        "deep thinking": "deep_think",
        "search": "web_search",
    }

    def _tool_allowed_for_mode(self, name: str) -> bool:
        """
        Check the confirmed tab_toggle_matrix in config to decide whether the
        requested tool is available on the currently-active mode.

        Returns True  → proceed (tool exists for this mode).
        Returns False → skip silently (tool is NOT present for this mode).

        If the matrix has no entry for the active mode (e.g. a future mode), we
        default to True so the scraper still *tries* rather than silently skips.
        """
        matrix = DEEPSEEK_CONFIG.get("tab_toggle_matrix", {})
        mode_entry = matrix.get(self._active_model_tab)
        if mode_entry is None:
            # Unknown mode — don't block, let the DOM check decide.
            return True
        matrix_key = self._TOOL_MATRIX_KEY.get(name.lower())
        if matrix_key is None:
            # Unknown tool name — don't block.
            return True
        return bool(mode_entry.get(matrix_key, False))

    async def _set_toggle(self, name: str, selectors: list[str], desired: bool) -> None:
        """
        Defensively enable a DeepSeek tool (DeepThink or Search). If the tool
        is absent or disabled in the currently-active mode, log accordingly
        and continue (do NOT crash).

        Matrix pre-check: before touching the DOM, consult tab_toggle_matrix to
        see if the tool is even supposed to exist on this mode. If not, skip
        silently (no warning — it's expected behaviour, not an error).
        """
        if not desired:
            return  # only act when caller wants it ON; default state is OFF

        # --- Pre-check: is this tool available on the active mode? ------------
        if not self._tool_allowed_for_mode(name):
            log.debug(
                "'%s' tool is not available on mode '%s' (confirmed by matrix) — "
                "skipping silently.",
                name, self._active_model_tab,
            )
            return

        loc = await self._find_first(selectors, timeout_ms=2500)
        if loc is None:
            # Tool should be present (matrix says so) but wasn't found —
            # this is a genuine selector mismatch worth warning about.
            log.warning(
                "'%s' tool not found in DOM for mode '%s'. "
                "Selector may need updating — check config.py selectors. "
                "Continuing without enabling '%s'.",
                name, self._active_model_tab, name,
            )
            return

        # Respect disabled state.
        try:
            disabled = (await loc.get_attribute("aria-disabled")) or ""
            if disabled.lower() == "true" or not await loc.is_enabled():
                log.warning(
                    "'%s' tool is disabled for mode '%s' — skipping.",
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
            log.info("Enabled '%s' tool", name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not enable '%s' tool: %s", name, exc)

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

        Order: (new -> open new chat) -> select mode (Instant/Expert/Vision) ->
        enable tools (DeepThink/Search) -> attach files -> type prompt -> send.
        Returns a handle string (here, the active response selector) for
        wait_for_response.
        """
        await self._ensure_loaded()

        if mode == "new":
            await self._goto_new_chat()
            # Select mode first — determines which tools are available.
            await self._select_model_tab(model_tab)
            # Enable tools — checked AFTER mode selection (availability differs per mode).
            await self._set_toggle("DeepThink", _SEL["deep_think_toggle"], deep_think)
            await self._set_toggle("Search", _SEL["web_search_toggle"], web_search)
        else:
            # CONTINUE mode: conversation is already in progress.
            # DeepSeek hides the mode pills and tool toggles once a conversation
            # has started — they are only shown on a fresh new-chat page.
            # Attempting _select_model_tab() / _set_toggle() here would time out
            # waiting for DOM elements that don't exist.
            # The mode and tools were set at the start of the conversation and
            # cannot be changed mid-thread; skip silently.
            log.debug(
                "CONTINUE mode: skipping mode/tool selection "
                "(controls are hidden in an active conversation)"
            )

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
