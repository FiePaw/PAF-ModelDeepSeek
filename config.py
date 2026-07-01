"""
config.py — Central configuration for PAF-ModelDeepSeek.

Defines all paths, browser config, DeepSeek DOM selectors, account rotation
phrases, output, and logging config. All directories are created automatically
on import.

NOTE ON SELECTORS
-----------------
DeepSeek (chat.deepseek.com) ships a React SPA whose DOM (class names, data
attributes) is minified and changes frequently. Every selector below that is
marked with `# TODO: verify` MUST be validated against the live site via
DevTools before running in production. Where possible we prefer *robust*
selectors (role, aria-label, placeholder text, visible text) over brittle
minified class names.
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent

COOKIES_DIR: Path = BASE_DIR / "cookies"
OUTPUT_DIR: Path = BASE_DIR / "output"
CODE_OUTPUT_DIR: Path = OUTPUT_DIR / "code"
LOGS_DIR: Path = BASE_DIR / "logs"
PROFILES_DIR: Path = BASE_DIR / "profiles"
DATA_SESSION_DIR: Path = BASE_DIR / "dataSession"
DEBUG_DIR: Path = BASE_DIR / "debug"

# Create every directory automatically on import.
for _d in (
    COOKIES_DIR,
    OUTPUT_DIR,
    CODE_OUTPUT_DIR,
    LOGS_DIR,
    PROFILES_DIR,
    DATA_SESSION_DIR,
    DEBUG_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Browser
# --------------------------------------------------------------------------- #
# A recent Chrome desktop user agent. Update periodically so it matches a real
# Chrome build (anti-bot systems flag stale UAs).
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_CONFIG: dict = {
    "headless": True,
    "slow_mo": 0,
    "viewport": {"width": 1280, "height": 800},
    "user_agent": USER_AGENT,
    "locale": "en-US",
    "timezone_id": "Asia/Jakarta",
    # Per-character typing delay (ms) to look human while typing the prompt.
    # OPTIMISATION: 15 -> 0. Key events are still dispatched per character (so
    # the site's input handlers fire), but without the artificial delay a long
    # prompt no longer costs len(prompt) * 15ms (e.g. 200 chars = 3s saved).
    "type_delay_ms": 0,
}

# Extra Chromium launch args used to reduce automation fingerprinting.
CHROMIUM_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--start-maximized",
]

PERSISTENT_CONTEXT_CONFIG: dict = {
    # When True the scraper uses launch_persistent_context() so cookies / local
    # storage / service workers persist on disk per-account. When False it uses
    # the ephemeral Browser + BrowserContext flow and re-injects cookies each
    # launch.
    "enabled": True,
    "default_profile": "default",
    "launch_args": CHROMIUM_LAUNCH_ARGS,
}


# --------------------------------------------------------------------------- #
# DeepSeek site config
# --------------------------------------------------------------------------- #
DEEPSEEK_CONFIG: dict = {
    "base_url": "https://chat.deepseek.com",
    "new_chat_url": "https://chat.deepseek.com",
    # The sign-in page used to detect an expired session.
    "login_url": "https://chat.deepseek.com/sign_in",

    # ------------------------------------------------------------------- #
    # SELECTORS — every one of these MUST be verified against the live DOM.
    # Multiple fallback selectors are provided per key (comma-joined CSS or a
    # list the scraper tries in order). Prefer text/placeholder/aria over
    # minified classes.
    # ------------------------------------------------------------------- #
    "selectors": {
        # Chat input textarea. #chat-input is the most stable selector
        # (confirmed across multiple community automation projects). Placeholder
        # reads "Message DeepSeek". TODO: re-verify if DeepSeek changes the id.
        "chat_input": [
            "textarea#chat-input",
            'textarea[placeholder*="Message DeepSeek" i]',
            'textarea[placeholder*="Message" i]',
            "div[contenteditable='true']",
        ],
        # Send button — up-arrow icon inside a circle at right of textarea.
        #
        # Verified DOM structure (June 2026):
        #   Parent container:  class="bf38813a"
        #   Button element:    class="ds-button ds-button--primary ds-button--filled
        #                             ds-button--circle ds-button--m
        #                             ds-button--icon-relative-m _52c986b"
        #   Inner element:     class="ds-button__background"
        #
        # Active   (input filled, ready to send):
        #   → classes above WITHOUT ds-button--disabled / bd74640a
        # Inactive (streaming / empty input):
        #   → adds ds-button--disabled + bd74640a to the class list
        #
        # Selector strategy: target the ACTIVE state only (no ds-button--disabled).
        # _52c986b and bf38813a are minified hashes — WILL change between builds.
        # Stable anchors: ds-button--circle, ds-button--primary, ds-button--filled.
        "send_button": [
            # Most precise: circle button inside verified container, not disabled
            ".bf38813a .ds-button--circle.ds-button--primary:not(.ds-button--disabled)",
            # Without container scope — still precise via circle+primary+not-disabled
            ".ds-button--circle.ds-button--primary.ds-button--filled:not(.ds-button--disabled)",
            # Broader fallback: any enabled circle ds-button
            ".ds-button--circle:not(.ds-button--disabled):has(.ds-button__background)",
            # Legacy fallbacks (pre-v2.0.3, kept for backward compat)
            'div[role="button"][aria-disabled="false"]:has(svg)',
            "div.ds-icon-button[aria-disabled='false']",
            'button[type="submit"]',
        ],
        # Attachment / paperclip button to the left of the send button.
        # TODO: verify
        "attach_button": [
            'div[aria-label*="attach" i]',
            "div.ds-icon-button:has(svg)",
            'div[class*="attach"]',
            'input[type="file"]',
        ],
        # Container holding rendered AI response markdown. DeepSeek uses the
        # ds- product namespace; .ds-markdown is the rendered response body.
        "response_container": [
            "div.ds-markdown",
            "div[class*='ds-markdown']",
            "div[class*='markdown']",
        ],
        # The latest assistant message specifically (last response bubble).
        "assistant_message": [
            "div.ds-markdown:last-of-type",
            "div[class*='ds-markdown']:last-of-type",
        ],
        # Virtual-list aware selectors — only elements CURRENTLY RENDERED in the
        # visible viewport. DeepSeek uses ds-virtual-list for long conversations:
        # elements outside the viewport are removed from the DOM entirely.
        # These selectors target only the visible slice, making text-change
        # detection immune to count fluctuations caused by virtual recycling.
        "virtual_list_response": [
            ".ds-virtual-list-visible-items div.ds-markdown",
            ".ds-virtual-list-visible-items div[class*='ds-markdown']",
            # Fallback: no virtual list (short conversations / fresh session)
            "div.ds-markdown",
            "div[class*='ds-markdown']",
        ],
        # "Typing"/generation in-progress indicator. TODO: verify.
        "loading_indicator": [
            "div[class*='loading']",
            "div[class*='_typing']",
            "svg[class*='spin']",
        ],
        # Stop-generation button shown while streaming. TODO: verify.
        "stop_button": [
            'div[role="button"][aria-label*="stop" i]',
            "div.ds-icon-button[aria-label*='stop' i]",
            'div[class*="stop"]',
        ],
        # "New chat" button in the top-left sidebar. TODO: verify.
        "new_chat_button": [
            'div[role="button"]:has-text("New chat")',
            "div.ds-icon-button:has-text('New chat')",
            'a[href="/"]',
        ],
        # ---------------- Layer 1: Mode selector (Instant / Expert / Vision) ---
        # DeepSeek has 3 modes shown as horizontal pills above the chat box.
        #
        # Verified DOM structure (minified React classes, June 2026):
        #   div.e362e944                              ← pill container
        #     div._9f2341b._18572c1[._31a22b0]        ← mode pill (active gets _31a22b0)
        #       div.dfb78875                          ← text label ("Instant"/"Expert"/"Vision")
        #
        # NO role="button", NO role="tab", NO "tab" substring in class names.
        # Active pill gains extra class _31a22b0 (minified — will change between builds).
        #
        # Selector strategy (ordered by specificity):
        #   1. :text-is("X")          — Playwright exact text match on the text label div.
        #                               Only matches elements whose FULL text content equals "X".
        #                               Will NOT match <html> or parent containers.
        #   2. div:has-text("X"):not(:has(div)) — leaf div (no div children) containing "X".
        #   3. div[class*="tab"]:has-text("X")  — fallback if future DOM adds "tab" in class.
        #   4. button / role="tab"               — semantic fallbacks for future refactors.
        #
        # IMPORTANT: NEVER use bare ':has-text("X") >> nth=0' — it matches <html> root
        # because <html> CONTAINS the text. The scraper then tries to click <html>
        # which is "not visible" → timeout.
        #
        # NOTE: The internal parameter name is "model_tab" for backward
        # compatibility, but user-facing terminology is "mode".
        "model_tab": {
            "instant": [
                ':text-is("Instant")',
                'div:has-text("Instant"):not(:has(div))',
                'div[class*="tab"]:has-text("Instant")',
                'button:has-text("Instant")',
                'div[role="tab"]:has-text("Instant")',
            ],
            "expert": [
                ':text-is("Expert")',
                'div:has-text("Expert"):not(:has(div))',
                'div[class*="tab"]:has-text("Expert")',
                'button:has-text("Expert")',
                'div[role="tab"]:has-text("Expert")',
            ],
            "vision": [
                ':text-is("Vision")',
                'div:has-text("Vision"):not(:has(div))',
                'div[class*="tab"]:has-text("Vision")',
                'button:has-text("Vision")',
                'div[role="tab"]:has-text("Vision")',
            ],
        },
        # Marker that indicates a mode pill is the active one.
        # DeepSeek uses an extra minified class on the active pill (e.g. _31a22b0)
        # which changes between builds. The hint "active" is a best-effort class
        # fragment check. The scraper also checks aria-checked / aria-pressed /
        # aria-selected, and has a class-count heuristic (active pill has MORE
        # classes than inactive siblings).
        "active_marker_class_hint": "active",
        # Additional aria attribute checked for active state (mode pill variant).
        "active_aria_selected": "aria-selected",

        # ---------------- Layer 2: Tools (DeepThink / Search) -----------------
        # DeepSeek has 2 tools, shown as toggle pills below the textarea.
        # Each is an independent on/off toggle.
        #
        # CONFIRMED availability matrix (verified from UI + user description):
        #   Instant mode : DeepThink ✅  Search ✅  (both tools available)
        #   Expert mode  : DeepThink ✅  Search ❌  (Search pill absent/hidden)
        #   Vision mode  : DeepThink ✅  Search ❌  (Search pill absent/hidden)
        #
        # The scraper enforces this matrix in code — it will NOT attempt to
        # enable Search when on Expert/Vision mode (avoids the warning).
        #
        # Selector strategy: DeepSeek uses .ds-toggle-button for these pills.
        # Text labels from screenshot: "DeepThink" and "Search".
        # Broad fallbacks included for label/class drift.
        "deep_think_toggle": [
            '.ds-toggle-button:has-text("DeepThink")',
            '.ds-toggle-button:has-text("Deep thinking")',
            'div[class*="toggle"]:has-text("DeepThink")',
            'div[class*="toggle"]:has-text("Deep thinking")',
            '[aria-label*="DeepThink" i]',
            '[aria-label*="deep think" i]',
            'div[role="button"]:has-text("DeepThink")',
            'div[role="button"]:has-text("Deep thinking")',
            'button:has-text("DeepThink")',
            'button:has-text("Deep thinking")',
        ],
        "web_search_toggle": [
            '.ds-toggle-button:has-text("Search")',
            'div[class*="toggle"]:has-text("Search")',
            '[aria-label*="Search" i]',
            'div[role="button"]:has-text("Search")',
            'button:has-text("Search")',
        ],
        # Login form marker (used by is_session_expired).
        # TODO: verify
        "login_form": [
            'input[type="password"]',
            'div:has-text("Log in")',
            'form[class*="login"]',
        ],

        # ---------------- Login form fields (email + password) ----------------
        # Matches the DeepSeek sign-in page: a "Phone number / email address"
        # field, a "Password" field, and a "Log in" button. TODO: verify.
        "login": {
            "email_input": [
                'input[placeholder*="email" i]',
                'input[placeholder*="Phone number" i]',
                'input[type="text"]:not([type="password"])',
                'input[name="email"]',
            ],
            "password_input": [
                'input[type="password"]',
                'input[placeholder*="Password" i]',
                'input[name="password"]',
            ],
            "login_button": [
                'div[role="button"]:has-text("Log in")',
                'button:has-text("Log in")',
                'div.ds-button:has-text("Log in")',
                'button[type="submit"]',
            ],
            # Optional "agree to terms" checkbox (some regions show one).
            "agree_checkbox": [
                'input[type="checkbox"]',
                'div.ds-checkbox',
            ],
            # Login error toast / inline message (wrong password, etc.).
            "error_message": [
                'div[class*="error" i]',
                'div.ds-toast',
                'div[class*="toast" i]',
            ],
            # Anti-bot captcha / slider that may appear after submit.
            "captcha": [
                'div[class*="captcha" i]',
                'div[class*="slider" i]',
                'iframe[src*="captcha" i]',
            ],
        },
    },

    # ------------------------------------------------------------------- #
    # Layer model / toggle config (replaces Qwen's think_mode_labels).
    # ------------------------------------------------------------------- #
    "model_tabs": {
        "instant": "Instant",
        "expert": "Expert",
        "vision": "Vision",
    },
    "default_model_tab": "instant",
    "deep_think_default": False,
    "web_search_default": False,

    # TODO (MANUAL VALIDATION REQUIRED):
    #   The set of valid (model_tab x Layer-2 toggle) combinations is NOT fully
    #   documented. The project owner confirmed Layer-2 options differ per tab
    #   (e.g. DeepThink may be hidden/disabled on Vision, Expert may force
    #   DeepThink). deepseek_scraper.py implements *defensive* toggling: if a
    #   requested toggle is absent/disabled for the active tab it logs a warning
    #   and continues WITHOUT crashing. Fill in the confirmed matrix below once
    #   you have explored every tab.
    # Confirmed tool availability per mode (verified by user + UI screenshot).
    # Keys map mode name -> which tools are present in the DOM.
    # The scraper uses this matrix to skip tools that don't exist for the
    # active mode, preventing false-negative warnings and unnecessary waits.
    "tab_toggle_matrix": {
        "instant": {"deep_think": True,  "web_search": True},   # both tools available
        "expert":  {"deep_think": True,  "web_search": False},  # DeepThink only
        "vision":  {"deep_think": True,  "web_search": False},  # DeepThink only
    },

    # ------------------------------------------------------------------- #
    # Attachments. DeepSeek shows a paperclip; image input is tied to the
    # Vision tab. Whether non-image docs (PDF) are accepted MUST be verified.
    # ------------------------------------------------------------------- #
    "attachments": {
        # TODO: verify the actual accepted set on the live site.
        "supported_types": ["image/png", "image/jpeg", "image/webp", "image/gif"],
        # Upload via CDP clipboard + Ctrl+V paste (NOT <input type=file>).
        "use_clipboard_paste": True,
    },

    # ------------------------------------------------------------------- #
    # Timeouts (seconds). response_wait is large because Expert / DeepThink
    # reasoning can take a long time.
    # ------------------------------------------------------------------- #
    "timeouts": {
        "page_load": 60,
        "response_wait": 60,
        "stability_check": 2.0,      # how long content must be unchanged
        # OPTIMISATION: stability tail = stability_polls * poll_interval.
        # Was 4 * 0.8 = 3.2s of dead time AFTER every answer finished.
        # Now 2 * 0.5 = 1.0s -> ~2.2s saved per process while still requiring
        # the response text to be unchanged across two consecutive reads.
        "stability_polls": 2,        # consecutive stable polls required
        "poll_interval": 0.5,
        # OPTIMISATION: small UI settle pause between actions, halved again (0.2 -> 0.1).
        # Called ~4-5x per process -> ~0.5s saved vs previous 0.2 value.
        "between_actions": 0.1,
        # OPTIMISATION: shared budget cap for locating a UI element (mode pills,
        # tool toggles, etc.). Used by _find_first so a missing element no longer
        # burns a full multi-second timeout per selector candidate.
        "element_find": 1000,
    },
}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
# AUTH FLOW (profile-first, password-fallback)
# --------------------------------------------
#   1. ALL account credentials live in ONE file: cookies/auth.json
#      (see cookies/auth.json.example for accepted formats).
#   2. The FIRST run for an account logs in with email + password.
#   3. The browser session is saved in a PERSISTENT PROFILE at profiles/<account>/
#      (Chromium keeps cookies/localStorage there automatically).
#   4. Every later run reuses that profile. If the login DOM reappears (session
#      expired), the scraper logs in again with the credentials from auth.json.
#
# Credentials for an account are resolved in this order (never hard-coded):
#   1. Values passed explicitly to DeepSeekScraper(email=..., password=...).
#   2. The matching entry in cookies/auth.json (keyed by account name).
#   3. Environment variables DEEPSEEK_EMAIL / DEEPSEEK_PASSWORD (or a .env file).
AUTH_CONFIG: dict = {
    # The single credentials file holding every account's email + password.
    "auth_file": str(COOKIES_DIR / "auth.json"),
    "login_url": "https://chat.deepseek.com/sign_in",
    "env_email": "DEEPSEEK_EMAIL",
    "env_password": "DEEPSEEK_PASSWORD",
    # Seconds to wait for the post-login redirect to the chat UI.
    "login_wait": 60,
    # Settle time after a successful login before using the page.
    # OPTIMISATION: reduced from 3.0 -> 0.5. The chat input is already confirmed
    # present (via _find_first) before this sleep runs, so a 3s blind wait is
    # unnecessary. 0.5s gives the SPA a small buffer without blocking the flow.
    "post_login_settle": 0.5,
    # If a captcha/slider appears, password login cannot complete headlessly.
    # When True the scraper fails loudly so you re-run once with --no-headless
    # to solve it (the persistent profile remembers it afterwards).
    "fail_loud_on_captcha": True,
}


# --------------------------------------------------------------------------- #
# Account rotation — phrases MUST be re-researched for DeepSeek (NOT Alibaba).
# --------------------------------------------------------------------------- #
ROTATION_CONFIG: dict = {
    # Phrases that mean "rate limited" — try a browser restart first, then
    # rotate to the next account. TODO: verify exact DeepSeek copy.
    "rate_limit_phrases": [
        "server busy",
        "server is busy",
        "please try again later",
        "you've reached your",
        "you have reached your",
        "usage limit",
        "rate limit",
        "too many requests",
        "请稍后再试",          # "please try again later" (zh)
        "服务器繁忙",          # "server busy" (zh)
    ],
    # Phrases where we rotate the account immediately (no restart first).
    "rotate_immediately_phrases": [
        "you've reached your usage limit",
        "daily limit reached",
        "quota exceeded",
    ],
    # Session expired / logged out markers. TODO: verify.
    "session_expired_phrases": [
        "log in",
        "sign in",
        "session expired",
        "please log in again",
        "登录",                # "log in" (zh)
    ],
    # Page-crash markers (Playwright / Chromium error pages).
    "page_crash_phrases": [
        "aw, snap",
        "page crashed",
        "he's dead, jim",
        "out of memory",
    ],
    "max_retries_per_account": 2,
    # OPTIMISATION: reduced from 3.0 -> 1.0. Shorter retry pause while still
    # giving the server a moment to recover. Called up to 5x per flow so the
    # old value could add up to 15s of dead time on a bad run.
    "retry_delay": 1.0,
    "rotation_delay": 5.0,
    "max_browser_restarts": 3,
    "browser_restart_delay": 4.0,
    # How long (seconds) a session stays alive after its last use.
    # After this period, get() returns None and the file is deleted from disk.
    # Matches Qwen's default TTL.
    "session_ttl": 3600,
}


# --------------------------------------------------------------------------- #
# Output / logging
# --------------------------------------------------------------------------- #
OUTPUT_CONFIG: dict = {
    "json_indent": 2,
    "encoding": "utf-8",
    "timestamp_format": "%Y%m%d_%H%M%S",
    "save_code_blocks": True,
}

LOG_CONFIG: dict = {
    "level": "INFO",
    "encoding": "utf-8",
    "timestamp_format": "%Y-%m-%d %H:%M:%S",
    "file": str(LOGS_DIR / "paf_deepseek.log"),
    "max_bytes": 5 * 1024 * 1024,
    "backup_count": 5,
    "use_color": True,
    "use_emoji": True,
}


# --------------------------------------------------------------------------- #
# JSON API mode (prompt wrapping + structured response) — parity w/ Qwen
# --------------------------------------------------------------------------- #
# When enabled, every user prompt is wrapped in a [SYSTEM CONTEXT] / [USER
# REQUEST] envelope (see DeepSeekScraper._build_wrapped_prompt) instructing
# DeepSeek to reply with a single-line JSON envelope:
#
#   {"status":"success","choices":[{"index":0,"message":{"role":"assistant",
#    "content":"..."},"finish_reason":"stop"}]}
#
# or, when tool calling is requested:
#
#   {"status":"tool_calls","tool_calls":[{"id":"call_...","type":"function",
#    "function":{"name":"...","arguments":{...}}}]}
#
# The scraper parses this envelope, extracts content / tool_calls, and forwards
# OpenAI-compatible fields to the VPS. If DeepSeek replies with non-JSON text
# (e.g. it ignored the format), a corrective-feedback prompt is sent in the
# SAME conversation up to `max_corrective_retries` times before the attempt is
# treated as failed (and the outer scrape() retry/rotation logic takes over).
#
# NOTE: This relies on DeepSeek honouring the in-message [SYSTEM CONTEXT].
# Unlike Qwen, DeepSeek has no per-account "Custom Instruction" field, so the
# enforcement lives entirely in the wrapped prompt. Set "enabled": False to
# revert to the original plain-text markdown behaviour.
JSON_API_CONFIG: dict = {
    "enabled": True,
    # Max in-session corrective retries when the response is not valid JSON.
    "max_corrective_retries": 2,
    # Base [SYSTEM CONTEXT] instruction used for plain chat (no tools).
    "chat_system_instruction": (
        "You are operating as a JSON API endpoint, NOT a conversational "
        "assistant.\n"
        "Respond with ONE single line of VALID JSON only — no markdown, no "
        "code fences, no explanations, and no text outside the JSON.\n"
        "\n"
        "RESPONSE FORMAT (use EXACTLY this schema):\n"
        '{"status":"success","choices":[{"index":0,"message":{"role":'
        '"assistant","content":"<your full answer as a string>"},'
        '"finish_reason":"stop"}]}\n'
        "\n"
        "RULES:\n"
        "- content MUST be a JSON string. Escape any double quotes as \\\" and "
        "newlines as \\n.\n"
        "- finish_reason MUST be \"stop\".\n"
        "- Output ONE line only. Do NOT add any field outside the schema above."
    ),
}
