# PAF-ModelDeepSeek

An async, Playwright-based browser-automation scraper for **`chat.deepseek.com`**,
adapted from the architecture of `PAF-ModelQwen`. It lets you drive a real,
logged-in DeepSeek web session from code and — optionally — expose it through an
**OpenAI-compatible REST API** (`/v1/chat/completions`) so other apps can "call
DeepSeek" exactly like they call OpenAI, while browser automation does the work
behind the scenes.

> ⚠️ **This is browser automation, not an official API.** Automating
> `chat.deepseek.com` may violate DeepSeek's Terms of Service. Use for personal
> / research purposes and at your own risk.

---

## Architecture

```
[External Client] ── HTTP REST (OpenAI-compatible) ──> [vps_server.py]
                                                              │ (WebSocket)
                                                              v
                                                      [public.py (Local Worker)]
                                                              │ (BrowserPool)
                                                              v
                                                      [Playwright Chromium]
                                                              │
                                                              v
                                                       [chat.deepseek.com]
```

Two ways to run it:

1. **Standalone** (`main.py`) — single machine, single/batch prompts straight
   to DeepSeek. Great for testing and one-off scraping.
2. **Distributed** (`public.py` + `vps_server.py`) — a local worker (on a PC
   logged into DeepSeek) connects over WebSocket to a VPS that exposes the
   OpenAI-compatible API to the world.

---

## Project layout

```
PAF-ModelDeepSeek/
├── scrapers/
│   ├── __init__.py
│   ├── base_scraper.py        # BaseAIChatScraper — abstract, generic
│   ├── deepseek_scraper.py    # DeepSeekScraper — chat.deepseek.com specifics
│   └── utils.py               # logging, token counter, cookie converter
├── config.py                  # paths, browser, DOM selectors, rotation, logging
├── main.py                    # standalone CLI (single / batch / continue)
├── browser_pool.py            # BrowserPool — pre-warmed slot manager
├── public.py                  # local worker (WebSocket client)
├── newpublic_BETA.py          # worker + persistent SessionStore
├── PublicForward/
│   └── ForVPS/
│       ├── start.sh
│       └── vps_server.py      # FastAPI + WebSocket server (VPS side)
├── cookies/                   # auth.json (all account credentials)
├── profiles/                  # persistent browser profiles (auto)
├── dataSession/               # local session cache (auto)
├── output/code/               # extracted code blocks
├── logs/
├── requirements.txt
├── requirements_api.txt
└── README.md
```

---

## Installation

### 1. Worker / standalone machine (drives the browser)

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. VPS (API server only)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements_api.txt
# No Playwright on the VPS — it only bridges WebSocket <-> REST.
```

---

## Authentication (profile-first, password login)

The auth flow has four stages:

1. **One credentials file** — put every account's email + password in
   **`cookies/auth.json`** (see `cookies/auth.json.example`).
2. **First run logs in** with email + password on
   `https://chat.deepseek.com/sign_in`.
3. **The session is saved in a persistent browser profile** at
   **`profiles/<account>/`** (Chromium stores cookies + localStorage there
   automatically — no manual cookie export needed).
4. **Every later run reuses that profile.** If the login DOM reappears (fresh
   profile or expired session), the scraper logs in again automatically using
   the credentials from `cookies/auth.json`.

### `cookies/auth.json`

Holds all accounts. Each account maps 1:1 to a profile at `profiles/<name>/`.
Multiple accounts enable automatic rotation on rate-limit.

```json
{
  "accounts": [
    { "name": "account1", "email": "you@example.com",    "password": "pass1" },
    { "name": "account2", "email": "second@example.com",  "password": "pass2" }
  ]
}
```

A single-account shorthand also works: `{ "email": "...", "password": "..." }`.

Pick an account with `--account account2` (defaults to the first one).

### Credential resolution order

For the active account, credentials are resolved as:
1. Explicit `--email` / `--password` CLI flags (override).
2. The matching entry in `cookies/auth.json`.
3. `DEEPSEEK_EMAIL` / `DEEPSEEK_PASSWORD` env vars (or a `.env` file — copy
   `.env.example`).

> ⚠️ **Captcha / slider:** if DeepSeek shows an anti-bot captcha after submit,
> headless login can't finish. Re-run the account **once** with `--no-headless`
> to solve it manually — the persistent profile remembers the session, so future
> headless runs work without logging in again.

> 🔒 Credentials are **never hard-coded**. `cookies/auth.json`, `.env`, and
> `profiles/` are all git-ignored.

> 💡 **First-time setup:** `cp cookies/auth.json.example cookies/auth.json`, fill
> in your account(s), then run once with `--no-headless` to complete any captcha.

---

## Running — standalone mode

```bash
# single prompt (Instant tab)
python main.py --prompt "Explain async/await in Python"

# Expert tab + DeepThink + web search
python main.py --prompt "Latest on fusion energy" \
    --model-tab expert --deep-think --web-search

# batch from a file, 3 concurrent browsers, save code blocks separately
python main.py --prompts-file prompts.txt --concurrent 3 --save-code

# continue the previous conversation instead of opening a new chat
python main.py --prompt "now summarize that" --mode continue

# visible browser (debug / captcha / re-login)
python main.py --prompt "hi" --no-headless

# attach an image (requires the Vision tab)
python main.py --prompt "describe this" --model-tab vision --attach ./pic.png
```

Outputs land in `output/` (JSON) and `output/code/` (extracted code blocks).

---

## Running — distributed mode

### On the VPS

```bash
cd PublicForward/ForVPS
PAF_TOKEN=my-shared-secret ./start.sh --port 8000
# or directly:
python vps_server.py --port 8000 --token my-shared-secret
```

### On the local (browser) machine

```bash
python public.py --vps ws://VPS_IP:8000/ws/worker \
    --workers 2 --token my-shared-secret

# BETA variant with persistent session resume:
python newpublic_BETA.py --vps ws://VPS_IP:8000/ws/worker \
    --workers 2 --token my-shared-secret
```

### Calling the API (OpenAI-compatible)

```bash
curl http://VPS_IP:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
        "model": "deepseek-reasoner",
        "messages": [{"role": "user", "content": "Prove sqrt(2) is irrational"}],
        "deep_think": true,
        "web_search": false,
        "stream": false
      }'
```

Models exposed by `/v1/models`:

| Model id | Maps to |
|---|---|
| `deepseek-chat` | Instant tab, DeepThink off |
| `deepseek-reasoner` | Expert tab, DeepThink on |
| `deepseek-vision` | Vision tab |

Custom (non-OpenAI) request fields: `deep_think`, `web_search`, `model_tab`,
`session_id` (for `continue` affinity), `mode` (`new`/`continue`), `attachments`.
`stream: true` returns OpenAI-style SSE (`data: {...}\n\n` … `data: [DONE]`).

Use it from the OpenAI SDK by pointing `base_url` at `http://VPS_IP:8000/v1`.

---

## DeepSeek UI model: two independent control layers

DeepSeek does **not** have Qwen's single "think mode" dropdown. It has **two
independent layers**:

- **Layer 1 — model tabs** above the chat box: **`Instant`**, **`Expert`**,
  **`Vision`** (select with `--model-tab` / `model_tab`).
- **Layer 2 — toggle pills** below the textarea: **`DeepThink`** (newer builds
  label it "Deep thinking") and **`Search`** (`--deep-think` / `--web-search`).

> ⚠️ **Confirmed by the project owner:** the Layer-2 toggles available **differ
> per Layer-1 tab** (e.g. DeepThink may be hidden/disabled on `Vision`, `Expert`
> may force DeepThink). The exact matrix is **not yet fully documented**. The
> scraper toggles **defensively**: if a requested toggle is missing/disabled for
> the active tab it logs a warning and continues without crashing. Fill in
> `DEEPSEEK_CONFIG["tab_toggle_matrix"]` in `config.py` once you've explored each
> tab manually.

---

## ⚠️ Selectors require manual verification

DeepSeek ships a minified React SPA whose class names change frequently. The
selectors in `config.py` are seeded from community automation projects and the
*stable* hooks (`#chat-input`, `.ds-markdown`, `.ds-toggle-button`,
`.ds-icon-button`) but **must be verified** before production:

> DevTools → right-click the element → **Inspect** → copy a robust selector
> (prefer id / `aria-label` / visible text over minified classes).

Every selector that needs checking is marked `# TODO: verify` in `config.py`.

### Items the project owner still needs to validate manually

These could not be verified without an authenticated, interactive DeepSeek
session and **must be confirmed before production**:

1. **Layer-1 tab selectors** (`Instant`/`Expert`/`Vision`) — confirmed only by
   visible text; verify the active-state marker class.
2. **The Layer-1 × Layer-2 availability matrix** — which toggles exist/are
   enabled per tab (see warning above). Populate `tab_toggle_matrix`.
3. **Send button class** — `div.ds-icon-button._7436101` is reported but the
   `_7436101` hash is minified and will change.
4. **Loading / stop-generation indicators** — not verified; the response-wait
   logic uses content-stability polling so it's resilient, but precise
   indicators improve speed.
5. **Attachment support** — paperclip exists; image input is tied to the Vision
   tab. Whether **PDF/other docs** are accepted is unconfirmed
   (`DEEPSEEK_CONFIG["attachments"]["supported_types"]`).
6. **localStorage keys** — whether DeepSeek stores extra auth tokens in
   localStorage (see the Authentication section).
7. **Rate-limit / session-expired phrases** — seeded with likely DeepSeek copy
   (incl. zh strings) in `ROTATION_CONFIG`; confirm against real messages.
8. **Login form selectors** (`config.DEEPSEEK_CONFIG["selectors"]["login"]`) —
   email field, password field, "Log in" button, and the captcha/error markers.
   Seeded from the standard sign-in page; verify against the live form.

---

## Resilience features (carried over from the original architecture)

- **Persistent browser profiles** per account (`profiles/`).
- **Pre-warmed BrowserPool** — no cold-start per request; dead slots respawn and
  rotate accounts.
- **Auto-restart on crash** + **account rotation** on rate-limit (restart-first,
  then rotate).
- **Response-stability waiting** — waits until output stops changing (robust for
  streaming and long Expert/DeepThink reasoning; `response_wait` defaults to
  300s).
- **JSON repair** for malformed model output (`_repair_unescaped_quotes`,
  `_repair_tool_calls_arguments`).
- **Debug screenshots** saved to `debug/` on errors.
- **WebSocket auto-reconnect** with backoff in the workers.
- **Session affinity** — `continue`-mode requests with the same `session_id` are
  routed back to the same worker (and the BETA worker persists session state to
  `dataSession/`).

---

## Coding style

Modern async Python: type hints, `dataclass`, `ABC`/`abstractmethod`,
`asyncio.gather`, and `async with` context managers throughout.

---

## License / disclaimer

Provided as-is for educational and personal use. Respect DeepSeek's Terms of
Service and applicable laws. The authors are not responsible for misuse.
