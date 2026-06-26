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
    "type_delay_ms": 15,
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
        # Community projects report div.ds-icon-button._7436101 with an
        # aria-disabled attribute that gates click-readiness. The _7436101
        # hash is MINIFIED and WILL change — keep robust fallbacks first.
        # TODO: verify the current minified class via DevTools.
        "send_button": [
            'div[role="button"][aria-disabled="false"]:has(svg)',
            "div.ds-icon-button._7436101",
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
        # ---------------- Layer 1: model tabs (Instant / Expert / Vision) -----
        # Shown as 3 horizontal pills above the chat box before chatting.
        # TODO: verify — these are best matched by visible text.
        "model_tab": {
            "instant": [
                'div[role="button"]:has-text("Instant")',
                'button:has-text("Instant")',
            ],
            "expert": [
                'div[role="button"]:has-text("Expert")',
                'button:has-text("Expert")',
            ],
            "vision": [
                'div[role="button"]:has-text("Vision")',
                'button:has-text("Vision")',
            ],
        },
        # Marker that indicates a tab/pill is the active one (used to avoid
        # re-clicking an already-active control). Verify the exact active class.
        # TODO: verify
        "active_marker_class_hint": "active",

        # ---------------- Layer 2: toggle pills (DeepThink / Search) ----------
        # Located below the textarea. Each is an independent on/off toggle.
        # IMPORTANT: availability of these toggles DIFFERS per Layer-1 tab —
        # explore each tab manually before trusting these. The scraper degrades
        # gracefully (logs a warning) when a toggle is missing/disabled.
        # NOTE: DeepSeek renamed "DeepThink" -> "Deep thinking" in newer builds;
        # both labels are kept as fallbacks. The control is a .ds-toggle-button.
        # TODO: verify current label/class via DevTools.
        "deep_think_toggle": [
            '.ds-toggle-button:has-text("Deep thinking")',
            '.ds-toggle-button:has-text("DeepThink")',
            '[aria-label*="DeepThink" i]',
            'div[role="button"]:has-text("Deep thinking")',
            'div[role="button"]:has-text("DeepThink")',
            'div[role="button"]:has-text("R1")',
        ],
        "web_search_toggle": [
            '.ds-toggle-button:has-text("Search")',
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
    "tab_toggle_matrix": {
        # "instant": {"deep_think": True, "web_search": True},   # TODO verify
        # "expert":  {"deep_think": True, "web_search": True},   # TODO verify
        # "vision":  {"deep_think": False, "web_search": False},  # TODO verify
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
        "response_wait": 300,
        "stability_check": 2.0,      # how long content must be unchanged
        "stability_polls": 4,        # consecutive stable polls required
        "poll_interval": 0.8,
        "between_actions": 0.4,
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
    "post_login_settle": 3.0,
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
    "retry_delay": 3.0,
    "rotation_delay": 5.0,
    "max_browser_restarts": 3,
    "browser_restart_delay": 4.0,
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
