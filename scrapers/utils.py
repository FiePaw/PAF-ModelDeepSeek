"""
scrapers/utils.py — shared helpers: colored/emoji logging, token counter,
JSON helpers, and the Cookie-Editor -> Playwright cookie converter.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

from config import LOG_CONFIG, OUTPUT_CONFIG

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41m",  # red bg
}
_RESET = "\033[0m"
_LEVEL_EMOJI = {
    "DEBUG": "🔍",
    "INFO": "ℹ️ ",
    "WARNING": "⚠️ ",
    "ERROR": "❌",
    "CRITICAL": "🔥",
}


class PrettyFormatter(logging.Formatter):
    """Console formatter with optional color + emoji per level."""

    def __init__(self, use_color: bool = True, use_emoji: bool = True) -> None:
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt=LOG_CONFIG["timestamp_format"],
        )
        self.use_color = use_color
        self.use_emoji = use_emoji

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        prefix = ""
        if self.use_emoji:
            prefix = _LEVEL_EMOJI.get(record.levelname, "") + " "
        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelname, "")
            return f"{color}{prefix}{base}{_RESET}"
        return f"{prefix}{base}"


_CONFIGURED: set[str] = set()


def get_logger(name: str = "paf_deepseek") -> logging.Logger:
    """Return a configured logger (console + rotating file). Idempotent."""
    logger = logging.getLogger(name)
    if name in _CONFIGURED:
        return logger

    logger.setLevel(LOG_CONFIG.get("level", "INFO"))
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(
        PrettyFormatter(
            use_color=LOG_CONFIG.get("use_color", True),
            use_emoji=LOG_CONFIG.get("use_emoji", True),
        )
    )
    logger.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            LOG_CONFIG["file"],
            maxBytes=LOG_CONFIG.get("max_bytes", 5 * 1024 * 1024),
            backupCount=LOG_CONFIG.get("backup_count", 5),
            encoding=LOG_CONFIG.get("encoding", "utf-8"),
        )
        file_handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt=LOG_CONFIG["timestamp_format"],
            )
        )
        logger.addHandler(file_handler)
    except Exception:  # pragma: no cover - file logging is best-effort
        logger.warning("Could not attach rotating file handler.")

    _CONFIGURED.add(name)
    return logger


# --------------------------------------------------------------------------- #
# Token counter (rough estimate for OpenAI-compatible usage fields)
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """Very rough token estimate. ~4 chars per token, with a word-count floor."""
    if not text:
        return 0
    by_chars = len(text) / 4
    by_words = len(text.split())
    return max(1, int(max(by_chars, by_words)))


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #
def dump_json(data: Any, path: str | Path) -> Path:
    """Write `data` as pretty JSON using config indent/encoding. Returns path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding=OUTPUT_CONFIG["encoding"]) as f:
        json.dump(data, f, indent=OUTPUT_CONFIG["json_indent"], ensure_ascii=False)
    return p


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding=OUTPUT_CONFIG["encoding"]) as f:
        return json.load(f)


def to_json_str(data: Any) -> str:
    return json.dumps(data, indent=OUTPUT_CONFIG["json_indent"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Cookie-Editor -> Playwright cookie converter
# --------------------------------------------------------------------------- #
def cookie_editor_json_to_playwright(raw_cookies: list[dict]) -> list[dict]:
    """
    Convert a Cookie-Editor export (array of cookie objects) into the schema
    accepted by Playwright's `context.add_cookies()`.

    Handles DeepSeek specifics:
      * `ds_session_id` is a session cookie (no expirationDate, session=true,
        httpOnly, secure, sameSite="strict") — it MUST NOT be skipped just
        because it lacks expirationDate.
      * `expirationDate` (epoch float) -> `expires`. Session cookies get
        expires omitted (Playwright treats absent expires as a session cookie).
      * `sameSite`: Cookie-Editor may emit literal null -> map to "Lax" default;
        "strict"/"lax"/"none" (any case) map to the Playwright-required
        "Strict"/"Lax"/"None".
      * Drop `hostOnly` and `storeId` (Playwright rejects them), but use
        `hostOnly` to decide whether the domain keeps a leading dot.
    """
    converted: list[dict] = []
    for c in raw_cookies:
        name = c.get("name")
        value = c.get("value")
        if name is None or value is None:
            continue

        domain = c.get("domain", "")
        host_only = bool(c.get("hostOnly", False))
        if host_only and domain.startswith("."):
            domain = domain.lstrip(".")
        elif not host_only and domain and not domain.startswith("."):
            # Keep as-is; Playwright accepts both forms. Leading dot widens scope.
            pass

        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": _normalize_same_site(c.get("sameSite")),
        }

        is_session = bool(c.get("session", False))
        exp = c.get("expirationDate")
        # Only set expires for non-session cookies that actually have a date.
        if not is_session and exp is not None:
            try:
                cookie["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
        # Session cookies: omit `expires` entirely so Playwright treats them as
        # session cookies (do NOT drop the cookie itself!).

        converted.append(cookie)

    return converted


def _normalize_same_site(value: Any) -> str:
    """Map Cookie-Editor sameSite (incl. literal null) to Playwright's enum."""
    if value is None:
        return "Lax"
    v = str(value).strip().lower()
    if v in ("strict",):
        return "Strict"
    if v in ("none", "no_restriction", "unspecified"):
        # Playwright requires Secure for None; callers ensure secure where used.
        return "None"
    # "lax" and anything unknown -> Lax (safe default)
    return "Lax"


# --------------------------------------------------------------------------- #
# AuthStore — single credentials file (cookies/auth.json) for all accounts
# --------------------------------------------------------------------------- #
class AuthStore:
    """
    Loads ALL account credentials from a single file: cookies/auth.json.

    Accepted formats (all auto-detected):

      1. List of accounts:
         [
           {"name": "account1", "email": "a@x.com", "password": "..."},
           {"name": "account2", "email": "b@x.com", "password": "..."}
         ]

      2. Object with "accounts":
         {"accounts": [ {"name": "...", "email": "...", "password": "..."} ]}

      3. Single account object:
         {"email": "a@x.com", "password": "..."}        -> name "account1"

      4. Mapping name -> credentials:
         {"account1": {"email": "...", "password": "..."}, ...}

    Each account maps 1:1 to a persistent browser profile at profiles/<name>/.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._accounts: dict[str, dict[str, str]] = {}
        self._order: list[str] = []
        self._load()

    def _add(self, name: Optional[str], email: Optional[str],
             password: Optional[str]) -> None:
        name = name or f"account{len(self._order) + 1}"
        if name in self._accounts:
            return
        self._accounts[name] = {"email": email, "password": password}
        self._order.append(name)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        if isinstance(data, list):
            for a in data:
                if isinstance(a, dict):
                    self._add(a.get("name"), a.get("email"), a.get("password"))
        elif isinstance(data, dict) and isinstance(data.get("accounts"), list):
            for a in data["accounts"]:
                if isinstance(a, dict):
                    self._add(a.get("name"), a.get("email"), a.get("password"))
        elif isinstance(data, dict) and ("email" in data or "password" in data):
            self._add(data.get("name"), data.get("email"), data.get("password"))
        elif isinstance(data, dict):
            # mapping: name -> {email, password}
            for name, creds in data.items():
                if isinstance(creds, dict):
                    self._add(name, creds.get("email"), creds.get("password"))

    def account_names(self) -> list[str]:
        return list(self._order)

    def get(self, name: Optional[str]) -> Optional[dict[str, str]]:
        if name is None:
            return None
        return self._accounts.get(name)

    def first(self) -> Optional[str]:
        return self._order[0] if self._order else None

    def __len__(self) -> int:
        return len(self._order)
