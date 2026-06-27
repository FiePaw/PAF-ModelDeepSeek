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
        # Tracks whether we are inside an active conversation thread.
        # Set to True once a prompt has been sent (or when CONTINUE navigation
        # lands on an existing thread). Reset to False by _goto_new_chat().
        # Mirrors the same flag used by PAF-ModelQwen.
        self._conversation_started: bool = False

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
        """
        Navigate to a fresh chat page.

        Strategy (mirrors PAF-ModelQwen):
          1. Try the in-app "New chat" button — keeps the SPA warm, faster.
          2. Fall back to hard page.goto() if the button is absent.
        Always resets _conversation_started so the next send_prompt() knows it
        is starting a brand-new thread.
        """
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
        # CRITICAL: reset flag so the next send_prompt() treats this as turn 1.
        self._conversation_started = False
        log.debug("_goto_new_chat: landed on new-chat page, _conversation_started=False")

    async def _ensure_page_ready(self, mode: str) -> None:
        """
        Single navigation gate — mirrors PAF-ModelQwen exactly.

        NEW  → always call _goto_new_chat() to land on a fresh thread.
        CONTINUE + _conversation_started=True
             → verify we are still on a DeepSeek page (URL sanity check).
               If the page drifted away (e.g. due to a rotation/restart), fall
               back to a new chat rather than silently sending to the wrong page.
        CONTINUE + _conversation_started=False
             → treat as NEW (safe fallback for first turn after a restart).

        NOTE: no authentication checks here — that is handled at a higher
        level (base_scraper.scrape → ensure_authenticated). This keeps the
        method side-effect-free for the CONTINUE path, exactly as Qwen does.
        """
        if mode == "new" or not self._conversation_started:
            await self._goto_new_chat()
        else:
            # CONTINUE: verify the browser is still on a DeepSeek page.
            # If _conversation_started=True but something (rotation, restart,
            # auth redirect) navigated the browser away, we would silently send
            # the prompt to the wrong page — same bug Qwen guards against by
            # always doing the navigation in public.py before calling scrape().
            current_url = self.page.url if self.page else ""
            if not current_url or DEEPSEEK_CONFIG["base_url"] not in current_url:
                log.warning(
                    "_ensure_page_ready: CONTINUE requested but page is at '%s' "
                    "(not a DeepSeek conversation) — opening new chat as fallback.",
                    current_url[:80],
                )
                await self._goto_new_chat()
            else:
                log.debug(
                    "_ensure_page_ready: CONTINUE — page at %s, skipping navigation.",
                    current_url[:80],
                )

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

        OPTIMISATION: skip the DOM check entirely when _authenticated=True and
        the browser is still on a DeepSeek page. The DOM check (is_session_expired)
        queries multiple selectors on every request — expensive for a condition
        that almost never changes mid-session. We only do the full DOM check on
        the first call per browser session, after a rotation/restart (both reset
        _authenticated=False), or when the page URL has drifted off DeepSeek.
        """
        if self.page is None:
            return False

        # Fast path: already confirmed authenticated this session AND still on
        # a DeepSeek page. Skip the DOM check entirely.
        if self._authenticated:
            current_url = self.page.url or ""
            if DEEPSEEK_CONFIG["base_url"] in current_url:
                log.debug("ensure_authenticated: cache hit — skipping DOM check")
                return True
            # URL drifted (e.g. external redirect). Fall through to full check.
            log.debug(
                "ensure_authenticated: URL drifted to '%s' — re-checking", current_url[:80]
            )

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

        Order:
          _ensure_page_ready(mode)      ← sole navigation gate (Qwen-style)
          [NEW only] select mode tab    ← Instant / Expert / Vision
          [NEW only] enable tools       ← DeepThink / Search
          attach files (both modes)
          type prompt → send
        Returns the response selector string consumed by wait_for_response().

        FIX: _ensure_loaded() has been intentionally removed from here.
        Calling it inside send_prompt() introduced a second authentication check
        that could call login() → page.goto(login_url) AFTER the caller already
        navigated to a conversation URL, silently destroying the CONTINUE context.
        Qwen's send_prompt() does not call any auth/load helper — it only calls
        _ensure_page_ready(). We follow the same contract:
          • Initial load / auth → ChatClient.launch() or base_scraper.scrape()
          • Navigation gate    → _ensure_page_ready() (this function, line below)
        """
        # Sole navigation gate — decides new chat vs continue existing thread.
        await self._ensure_page_ready(mode)

        if mode == "new":
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
        # Use fill() for instant input instead of type() char-by-char.
        # A single short sleep after fill() is enough to let the SPA register
        # the input event before the send button is clicked.
        await input_loc.fill(prompt)
        await asyncio.sleep(BROWSER_CONFIG.get("fill_settle_ms", 120) / 1000)

        # Send: prefer clicking the send button; fall back to Enter.
        sent = await self._click_first(_SEL["send_button"], timeout_ms=4000)
        if not sent:
            log.info("Send button not clickable; pressing Enter as fallback")
            await input_loc.press("Enter")

        # Mark conversation as active so future CONTINUE calls skip navigation.
        self._conversation_started = True

        # Return the selector wait_for_response should poll.
        return _SEL["assistant_message"][0]

    # ------------------------------------------------------------------ #
    # Turn 2: tool-result injection (mirrors PAF-ModelQwen)
    # ------------------------------------------------------------------ #
    async def scrape_with_tool_result(
        self,
        tool_messages: list[dict],
        next_user_msg: Optional[str] = None,
    ) -> dict:
        """
        Inject tool results back into an existing CONTINUE conversation (Turn 2).

        Builds a structured prompt:

            [TOOL RESULT]
            {"tool_call_id": "...", "name": "...", "result": {...}}
            ... (one block per tool message)

            [USER REQUEST]              <- only if next_user_msg is provided
            {"prompt": "..."}

        Then calls send_prompt() in CONTINUE mode (no new-chat navigation) and
        waits for the model response exactly like a normal scrape() call.

        Returns a dict in the same shape as scrape():
          {"ok": True, "mode": "continue", "account": ..., "text": ..., ...}
        """
        import json as _json
        from datetime import datetime

        parts: list[str] = []

        for tm in tool_messages:
            # Normalise: accept both {"result": ...} and {"content": ...}
            result_val = tm.get("result") or tm.get("content") or ""
            if isinstance(result_val, (dict, list)):
                result_str = _json.dumps(result_val, ensure_ascii=False)
            else:
                result_str = str(result_val)

            block = {
                "tool_call_id": tm.get("tool_call_id", ""),
                "name":         tm.get("name", tm.get("tool_name", "")),
                "result":       result_str,
            }
            parts.append("[TOOL RESULT]\n" + _json.dumps(block, ensure_ascii=False))

        if next_user_msg:
            parts.append(
                "[USER REQUEST]\n"
                + _json.dumps({"prompt": next_user_msg}, ensure_ascii=False)
            )

        wrapped_prompt = "\n\n".join(parts)

        log.info(
            "scrape_with_tool_result: %d tool result(s), next_user=%s",
            len(tool_messages),
            repr(next_user_msg[:40]) if next_user_msg else "None",
        )

        # Reuse the same orchestration logic as scrape() but skip the full
        # retry loop — send directly and return.
        try:
            if self.page is None:
                await self.launch_browser(self.account)

            initial_response_count = await self._count_response_elements()
            await self._snapshot_baseline_text()
            await self.send_prompt(wrapped_prompt, mode="continue")

            from config import DEEPSEEK_CONFIG as _DS_CFG
            t = _DS_CFG["timeouts"]
            text = await self.wait_for_response(
                response_selectors=self._response_selectors(),
                timeout=t["response_wait"],
                stability_secs=t["stability_check"],
                stability_polls=t["stability_polls"],
                poll_interval=t["poll_interval"],
                initial_response_count=initial_response_count,
            )

            ok, cleaned = self._validate_response(text)
            if not ok:
                cleaned = self._repair_unescaped_quotes(cleaned)
                cleaned = self._repair_tool_calls_arguments(cleaned)

            return {
                "ok":          True,
                "mode":        "continue",
                "account":     self.account,
                "text":        cleaned,
                "code_blocks": self.extract_code_blocks(cleaned),
                "timestamp":   datetime.now().astimezone().isoformat(),
            }
        except Exception as exc:
            log.error("scrape_with_tool_result error: %s", exc, exc_info=True)
            await self.take_debug_screenshot("tool_result_error")
            return {
                "ok":        False,
                "mode":      "continue",
                "account":   self.account,
                "error":     str(exc),
                "timestamp": datetime.now().astimezone().isoformat(),
            }

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
        """
        Fast check: are we on / showing the login DOM?

        FIX: removed 'div:has-text("Log in")' from the login-form selector
        check. Playwright's :has-text() matches ANY element whose subtree
        contains the text — so on a logged-in conversation page the sidebar
        "Log in" link, a referral banner, or any help text would trigger a
        false positive, causing ensure_authenticated() → login() to navigate
        away from the conversation URL and break CONTINUE mode.

        Detection order (cheapest → safest):
          1. URL is the sign-in URL             → definitely expired
          2. input[type="password"] is VISIBLE  → login form showing
          3. Chat input present                 → definitely authenticated
          4. Body contains session-expiry phrase → expired
        """
        if self.page is None:
            return False

        # 1) On the sign-in page URL (instant, no DOM query needed).
        if DEEPSEEK_CONFIG["login_url"] in (self.page.url or ""):
            return True

        # 2) Login form: only check for a VISIBLE password field.
        #    This is far more specific than :has-text() and will not fire on
        #    logged-in pages that happen to mention "Log in" somewhere.
        try:
            pwd = await self.page.query_selector('input[type="password"]')
            if pwd is not None and await pwd.is_visible():
                return True
        except Exception:
            pass

        # 3) Chat input present → definitely authenticated (fast exit).
        for sel in _SEL["chat_input"]:
            try:
                if await self.page.query_selector(sel) is not None:
                    return False
            except Exception:
                continue

        # 4) Phrase match on body text (last resort).
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
    # Rotation / restart overrides — reset conversation state
    # ------------------------------------------------------------------ #
    async def _rotate_account(self, restart_first: bool = True) -> bool:
        """
        Override: reset _conversation_started after rotating.

        After rotation the browser lands on the new account's home page, NOT
        on the previous conversation URL. If we kept _conversation_started=True,
        _ensure_page_ready("continue") would skip navigation and the next prompt
        would be sent to the home page, silently starting a new conversation.

        Mirrors the implicit reset in Qwen: _rotate_account() always calls
        launch_browser / close/reopen, which navigates to the home page; Qwen's
        next call to _ensure_page_ready("new") or scrape(mode="new") then calls
        _goto_new_chat() which resets the flag explicitly.
        """
        result = await super()._rotate_account(restart_first=restart_first)
        if result:
            self._conversation_started = False
            log.debug(
                "_rotate_account: account rotated — _conversation_started reset to False"
            )
        return result

    async def restart_browser(self, account=None) -> "Page":
        """
        Override: reset _conversation_started on browser restart.

        After a restart the page is fresh (home or about:blank). Keeping the
        flag True would cause the same wrong-page bug as after rotation.
        """
        self._conversation_started = False
        log.debug(
            "restart_browser: restarting — _conversation_started reset to False"
        )
        return await super().restart_browser(account)

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