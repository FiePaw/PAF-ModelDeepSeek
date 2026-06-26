# Changelog — PAF-ModelDeepSeek

All notable changes to this project are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [2.0.5] 2026-06-26 — Update /v1/models response format

### Changes

#### `PublicForward/ForVPS/vps_server.py` — `/v1/models` endpoint

- `owned_by` changed from `"deepseek"` to `"PAF-ai"`
- Removed unused fields: `created`, `permission`, `root`, `parent`
- Response `data` now sourced from `worker_mgr.list_all_accounts()` (live
  accounts from connected workers) instead of `MODEL_ALIASES.keys()`
- Removed extra `"accounts"` field from response root (not part of OpenAI spec)

New response shape:
```json
{
  "object": "list",
  "data": [
    {"id": "account1", "object": "model", "owned_by": "PAF-ai"},
    {"id": "account2", "object": "model", "owned_by": "PAF-ai"}
  ]
}
```

#### `API_USAGE.md`

- Updated `/v1/models` response example to match new format
- Removed `created` field from example
- Updated `owned_by` from `"deepseek"` to `"PAF-ai"`
- Updated notes section

### Files Changed

| File | Change |
|------|--------|
| `PublicForward/ForVPS/vps_server.py` | `/v1/models` — simplified response, `owned_by` → `"PAF-ai"` |
| `API_USAGE.md` | Updated `/v1/models` example and notes |
| `CHANGELOG.md` | Added this entry |

---

## [2.0.4] 2026-06-26 — Fix CONTINUE mode stale response + send button DOM

### Bug Fixes

#### CONTINUE mode: scraper returned old response instead of new one

**Root cause (three layers):**

1. `_read_latest_response()` always used `nth(count - 1)` (the last element on
   page). In CONTINUE mode the page already has N old `div.ds-markdown`
   elements. When polling began, `nth(count - 1)` pointed to the last *old*
   response — which was already stable — so `wait_for_response` hit
   `stability_polls` immediately and returned the stale text before the new
   response had even started streaming.

2. `wait_for_response()` had no notion of how many responses existed before the
   prompt was sent. It could not distinguish "old stable response" from "new
   stable response".

3. `send_prompt()` called `_select_model_tab()` and `_set_toggle()` in CONTINUE
   mode, but DeepSeek hides the mode pills and tool toggles once a conversation
   is in progress. These calls would time out waiting for DOM elements that no
   longer exist, wasting seconds and producing warning logs on every CONTINUE
   request.

**Fix (`scrapers/base_scraper.py`):**

- Added `_count_response_elements() -> int`:
  Counts `div.ds-markdown` elements currently on page using the same selector
  chain as `_response_selectors()`. Called immediately **before** `send_prompt`
  to snapshot the baseline.

- `wait_for_response()` gains `initial_response_count: int = 0` parameter:
  Passed through to `_read_latest_response()` as `skip_count`. Defaults to 0
  so NEW mode behaviour is completely unchanged.

- `_read_latest_response()` gains `skip_count: int = 0` parameter:
  Now guards `count > skip_count` before reading. If the new response has not
  yet appeared (count is still at baseline), returns `""` — keeping
  `stable_count` at 0 and forcing the loop to keep waiting. Once the new
  element appears (`count > skip_count`), reads `nth(count - 1)` as before.

- `scrape()` snapshots baseline before `send_prompt` and passes it to
  `wait_for_response`.

**Fix (`scrapers/deepseek_scraper.py`):**

- `send_prompt()`: mode/tool selection (`_select_model_tab`, `_set_toggle`)
  is now gated to `mode == "new"` only. In CONTINUE mode these controls are
  absent from the DOM; skipping them eliminates spurious timeouts and log
  warnings. A `log.debug` note is emitted instead.

#### Send button DOM selectors (config.py)

**Root cause:**
Send button was identified by `div.ds-icon-button._7436101` where `_7436101`
is a minified hash that changes between DeepSeek builds. The selector was
already stale; the button was being found (if at all) only via the
`div[role="button"][aria-disabled="false"]:has(svg)` fallback.

**Verified DOM structure (June 2026):**
```
<div class="bf38813a">                         ← parent container
  <button class="ds-button ds-button--primary
                 ds-button--filled ds-button--circle
                 ds-button--m ds-button--icon-relative-m
                 _52c986b">                    ← ACTIVE: no ds-button--disabled
    <div class="ds-button__background"/>
    …svg icon…
  </button>
</div>
```
Inactive (streaming / empty input) adds `ds-button--disabled bd74640a` to the
button class list.

**Fix (`config.py`) — new `send_button` selector priority:**

| # | Selector | Notes |
|---|----------|-------|
| 1 | `.bf38813a .ds-button--circle.ds-button--primary:not(.ds-button--disabled)` | Most precise — scoped to container |
| 2 | `.ds-button--circle.ds-button--primary.ds-button--filled:not(.ds-button--disabled)` | No container scope, still stable |
| 3 | `.ds-button--circle:not(.ds-button--disabled):has(.ds-button__background)` | Broader fallback |
| 4 | `div[role="button"][aria-disabled="false"]:has(svg)` | Legacy (pre-v2.0.4) |
| 5 | `div.ds-icon-button[aria-disabled="false"]` | Legacy |
| 6 | `button[type="submit"]` | Last-resort semantic fallback |

Active/inactive distinction is now done via `:not(.ds-button--disabled)` —
no longer depends on `aria-disabled` or minified class hashes.

Minified hashes `_52c986b` and `bf38813a` are documented as volatile in
comments.

### Files Changed

| File | Change |
|------|--------|
| `scrapers/base_scraper.py` | Added `_count_response_elements()`, updated `wait_for_response()` + `_read_latest_response()` signatures, snapshot in `scrape()` |
| `scrapers/deepseek_scraper.py` | `send_prompt()` skips mode/tool selection in CONTINUE mode |
| `config.py` | `send_button` selectors replaced with verified June 2026 DOM selectors |
| `CHANGELOG.md` | Restored file (was accidentally overwritten in a prior commit); added v2.0.3 and v2.0.4 entries |

---

## [2.0.3] 2026-06-26 — Fix think_mode alias + mode selector strategy

### Bug Fixes

#### think_mode alias: "search" routed to Expert instead of Instant

**Root cause (`PublicForward/ForVPS/vps_server.py`):**
`THINK_MODE_ALIASES` was missing the `"search"` key entirely. VPS fell through
to a default that mapped it to Expert mode. Search tool is only available on
Instant — so `web_search=True` was silently ignored every request.
Also missing: `"deep"` alias for Expert mode.

**Fix:**
- `"search"` → Instant mode + `web_search=True`
- `"deep"` → Expert mode + `deep_think=True`
- `"auto"` / `"instant"` / `"fast"` → Instant, no tools
- `"thinking"` / `"expert"` / `"reasoning"` → Expert + DeepThink
- `"vision"` → Vision, no tools

#### Mode selector timed out (matched `<html>` root)

**Root cause (`config.py`):**
Mode pills were selected with `':has-text("Instant") >> nth=0'`. Playwright's
`:has-text()` without scope matches all ancestor elements including `<html>`.
`nth=0` therefore returned the root element, which was never interactable →
timeout on every request.

**Fix:**
Replaced with ordered fallback selector stack per mode:
1. `:text-is("Instant")` — Playwright exact-text, no ancestor bleed
2. `div:has-text("Instant"):not(:has(div))` — leaf-div fallback
3. `div[class*="tab"]:has-text("Instant")` — class-fragment fallback
4. `button:has-text("Instant")` — semantic button fallback
5. `div[role="tab"]:has-text("Instant")` — ARIA fallback

Added `get_by_text(label, exact=True) → locator('..') → click parent`
fallback in `_select_model_tab()` for when all CSS selectors miss.

Added `_is_active_by_class_count()`: compares CSS class count of target
pill vs its siblings. Active pill has one extra minified class; inactive
siblings have fewer. Robust to minified class name changes.

### Files Changed

| File | Change |
|------|--------|
| `config.py` | Mode selectors rewritten; `tab_toggle_matrix` filled; terminology updated |
| `scrapers/deepseek_scraper.py` | `_select_model_tab()` fallback; `_is_active_by_class_count()`; terminology |
| `PublicForward/ForVPS/vps_server.py` | `THINK_MODE_ALIASES` fixed; version → 2.0.3 |
| `CHANGELOG.md` | Added this entry |
| `API_USAGE.md` | Corrected `think_mode` table; added Tool Availability info box; added Example 4b |

### Version comparison

| Feature | v2.0.2 | v2.0.3 | v2.0.4 | v2.0.5 |
|---------|--------|--------|--------|--------|
| Tab selection | `:has-text >> nth=0` (broken) | `:text-is()` + fallbacks | — | — |
| Tool matrix | TODO placeholder | Confirmed matrix | — | — |
| `think_mode` aliases | Incomplete, "search" wrong | All aliases correct | — | — |
| Send button selector | `_7436101` (stale hash) | — | Stable `ds-button--` classes | — |
| CONTINUE mode response | Returns old response | — | Anchored to post-send count | — |
| CONTINUE mode DOM calls | Mode/tool selectors called | — | Skipped (controls hidden) | — |
| `/v1/models` `owned_by` | `"deepseek"` | — | — | `"PAF-ai"` |
| `/v1/models` fields | Full OpenAI schema | — | — | Minimal `id/object/owned_by` |
| `/v1/models` data source | `MODEL_ALIASES.keys()` | — | — | `list_all_accounts()` |

---

## [2.0.2] and earlier

See git history (`git log --oneline`) for changes prior to v2.0.3.
