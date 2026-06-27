# Changelog — PAF-ModelDeepSeek

All notable changes to this project are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [2.1.0] 2026-06-27 — Session persistence overhaul, Turn 2 tool-result, CONTINUE mode bug fixes

Rilis ini membawa arsitektur session baru yang sepenuhnya mengadopsi pola
PAF-ModelQwen, memperbaiki bug sistematis pada CONTINUE mode, dan menambahkan
Turn 2 (tool-result injection). File `newpublic_BETA.py` dihapus — semua
fiturnya sudah dilebur ke `public.py` dan `browser_pool.py`.

---

### New Features

#### Session Persistence — `Session` dataclass + `SessionStore` yang di-upgrade (`public.py`)

Sebelumnya `SessionStore` hanya menyimpan JSON mentah tanpa TTL, tanpa restore
saat startup, dan tanpa account pinning. Kini diganti penuh dengan:

- **`Session` dataclass** — fields: `session_id`, `account`, `conversation_url`,
  `created_at`, `last_used`, `turn_count`
- **TTL-aware `get()`** — auto-hapus session expired dari memory + disk
- **`load_from_disk()`** — restore semua session non-expired saat startup
  (conversation bisa dilanjutkan setelah worker restart)
- **`cleanup_expired()`** — background pruning memory + disk
- **`bump_turn()`** — increment `turn_count` setiap turn berhasil
- **`get_or_create()`** dan **`update()`** — upsert atomic dengan persist ke disk
- **Account pinning** — `Session.account` disimpan → CONTINUE selalu diarahkan
  ke account yang sama dengan Turn 1

#### Background cleanup loop (`public.py`)

- `LocalWorker._cleanup_loop()` — asyncio task berjalan setiap 60 detik,
  memanggil `SessionStore.cleanup_expired()` dan `_cleanup_session_locks()`
- `_session_locks_meta` — timestamp per-lock; lock idle >1 jam di-GC otomatis
- `--session-ttl` CLI flag — konfigurasi TTL session (default: 3600 detik)

#### Turn 2 — Tool-result injection

CONTINUE mode kini mendukung multi-turn dengan tool calls:

**`scrapers/deepseek_scraper.py` — `scrape_with_tool_result()`**

Membangun prompt terstruktur dan mengirimnya ke thread yang sedang berjalan:
```
[TOOL RESULT]
{"tool_call_id": "c1", "name": "write_file", "result": "done"}

[USER REQUEST]        ← opsional (next_user_msg)
{"prompt": "sekarang jalankan"}
```

**`browser_pool.py` — `run_task_with_tool_result()`**

Mengambil slot dengan account yang sama (pinned), re-navigasi ke conversation
URL jika perlu, lalu memanggil `scrape_with_tool_result()`. Selalu release
dengan `reset=False` untuk menjaga page tetap hidup.

**`public.py` — `_execute_task()`**

Mendeteksi `tool_messages` dalam payload → routing ke
`pool.run_task_with_tool_result()` instead of `pool.run_task()`.

#### `BrowserPool` — skip-goto optimisation + `release(reset=)` (`browser_pool.py`)

- **`acquire_pinned(slot_index)`** — ambil slot spesifik berdasarkan index
  (untuk CONTINUE pinning); fallback ke idle slot jika tidak tersedia
- **`release(slot, reset=True)`** — `reset=False` pada CONTINUE: page tetap
  hidup di URL conversation, siap untuk turn berikutnya tanpa reload
- **Skip-goto optimisation** di `run_task()`: bandingkan `page.url` dengan
  `conversation_url`; jika sudah sama, set `_conversation_started=True` dan
  skip `page.goto()` (hemat 2–6 detik)
- Navigation diganti dari `networkidle` ke `domcontentloaded + asyncio.sleep(2)`
  — lebih reliable untuk SPA DeepSeek yang tidak pernah benar-benar mencapai
  `networkidle`
- `conversation_url` di-attach otomatis ke result dict setelah setiap turn

#### `_ensure_page_ready()` — single navigation gate (`scrapers/deepseek_scraper.py`)

Mengadopsi pola PAF-ModelQwen secara penuh:

```python
async def _ensure_page_ready(self, mode: str) -> None:
    if mode == "new" or not self._conversation_started:
        await self._goto_new_chat()   # NEW → fresh thread
    else:
        # CONTINUE → URL sanity check (lihat Bug Fixes)
        if base_url not in page.url:
            await self._goto_new_chat()  # fallback jika page drift
```

- `_conversation_started: bool` — flag instance baru di `__init__`
- `_goto_new_chat()` selalu reset `_conversation_started = False`
- `send_prompt()` menjadikan `_ensure_page_ready()` sebagai **satu-satunya**
  navigation entry point

#### `scrape_with_tool_result()` — Turn 2 pada standalone scraper (`scrapers/deepseek_scraper.py`)

Dapat dipakai langsung dari `main.py` atau `chat.py` tanpa melalui pool/worker.

#### CLI session persistence (`main.py`)

`--mode continue` sebelumnya tidak menyimpan atau memuat conversation URL —
setiap run selalu membuka chat baru meskipun mode "continue".

- **`_save_cli_session(session_id, url, account)`** — simpan ke
  `dataSession/cli_<id>.json` setelah setiap run berhasil
- **`_load_cli_session(session_id)`** — muat URL + account sebelum scraping
- **`--session-id`** — nama sesi CLI (default: `"cli"`)
- Account pinning: CONTINUE menggunakan account yang sama dengan turn pertama
- Skip-goto optimisation: jika browser sudah di conversation URL, skip `goto()`

#### `examples/chat.py` — Interactive chat client via HTTP API (file baru)

Client interaktif multi-turn yang memanggil `/v1/chat/completions` langsung.
Tidak ada dependency ke modul internal — hanya butuh `requests`.

```bash
python examples/chat.py
python examples/chat.py --session-id riset-1
python examples/chat.py --think-mode thinking
python examples/chat.py --base-url http://192.168.1.10:9000
```

Fitur:
- Auto-manage `mode`: turn 1 → `"new"`, turn 2+ → `"continue"`
- Health check saat startup
- Commands: `/new [id]`, `/status`, `/think <mode>`, `/help`, `/quit`
- Prompt indicator: `●` = sedang dalam thread, `○` = belum mulai

---

### Bug Fixes

#### CONTINUE mode: prompt dikirim ke halaman baru alih-alih melanjutkan thread

**Root cause (5 lapisan — ditemukan dari analisis mendalam PAF-ModelQwen):**

| # | Lokasi | Masalah |
|---|--------|---------|
| 1 | `send_prompt()` | `_ensure_loaded()` dipanggil → `ensure_authenticated()` → `login()` → `page.goto(login_url)` — page pindah dari conversation URL sebelum `_ensure_page_ready()` sempat menjaga |
| 2 | `is_session_expired()` | Selector `div:has-text("Log in")` mencocokkan **semua** elemen yang mengandung teks "Log in" di subtree-nya — termasuk tombol sidebar, referral banner, dsb. di conversation page → false positive sistematis |
| 3 | `_ensure_page_ready()` | Tidak ada URL sanity check: jika `_conversation_started=True` tapi page sudah drift ke URL lain, navigation dilewati dan prompt dikirim ke halaman yang salah |
| 4 | `_rotate_account()` | Tidak mereset `_conversation_started` → setelah rotasi, browser di home page akun baru tapi flag masih `True` → `_ensure_page_ready("continue")` skip goto → prompt ke home page |
| 5 | `restart_browser()` | Sama dengan #4: `_conversation_started` tidak direset setelah restart |

**Fixes (`scrapers/deepseek_scraper.py`):**

- **Fix 1** — Hapus `_ensure_loaded()` dari `send_prompt()`. Sesuai arsitektur
  Qwen: `send_prompt()` tidak pernah melakukan auth check. Auth sudah ditangani
  oleh `base_scraper.scrape()` dan `ChatClient.launch()`.

- **Fix 2** — `is_session_expired()` diperketat:

  ```python
  # SEBELUM (false positive):
  for sel in _SEL["login_form"]:   # termasuk 'div:has-text("Log in")'
      if await page.query_selector(sel): return True

  # SESUDAH (spesifik):
  pwd = await page.query_selector('input[type="password"]')
  if pwd and await pwd.is_visible(): return True   # hanya login form nyata
  ```

- **Fix 3** — `_ensure_page_ready()` CONTINUE path kini memverifikasi URL:
  jika `base_url` tidak ada di `page.url` → fallback ke `_goto_new_chat()`

- **Fix 4 & 5** — Override `_rotate_account()` dan `restart_browser()` di
  `DeepSeekScraper` untuk reset `_conversation_started = False`:

  ```python
  async def _rotate_account(self, restart_first=True) -> bool:
      result = await super()._rotate_account(restart_first)
      if result:
          self._conversation_started = False   # ← baru
      return result
  ```

**Fix tambahan (`chat.py`):**

- `ChatClient.send()` kini memverifikasi URL sebelum setiap CONTINUE turn —
  mirror dari skip-goto optimisation Qwen di `public.py`:

  ```python
  if _conversation_started and session.conversation_url:
      already_there = conv_url in page.url or page.url in conv_url
      if not already_there:
          await page.goto(conv_url)   # re-navigate jika drift
          _conversation_started = True
  ```

---

### Removed

- **`newpublic_BETA.py`** — Dihapus. Semua fiturnya (`auto_continue`,
  slot pinning, `bump_turn`, `load_from_disk`) sudah dilebur ke `public.py`
  dan `browser_pool.py`.

---

### Files Changed

| File | Perubahan |
|------|-----------|
| `config.py` | Tambah `session_ttl: 3600` ke `ROTATION_CONFIG` |
| `public.py` | `Session` dataclass; `SessionStore` TTL/disk/bump_turn/account-pin; `LocalWorker` cleanup loop, lock GC, `--session-ttl`, Turn 2 dispatch |
| `browser_pool.py` | `acquire_pinned()`, `release(reset=)`, `run_task_with_tool_result()`, skip-goto, domcontentloaded nav, auto-attach `conversation_url` |
| `scrapers/deepseek_scraper.py` | `_conversation_started` flag; `_ensure_page_ready()`; `_goto_new_chat()` reset; `send_prompt()` tanpa `_ensure_loaded()`; `scrape_with_tool_result()`; fix `is_session_expired()`; override `_rotate_account()` + `restart_browser()` |
| `main.py` | `_save_cli_session()`, `_load_cli_session()`, `--session-id`, account pinning, skip-goto untuk `--mode continue` |
| `examples/chat.py` | **Baru** — interactive chat client via HTTP API |
| `newpublic_BETA.py` | **Dihapus** |
| `CHANGELOG.md` | Entri ini |

---



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

| Feature | v2.0.2 | v2.0.3 | v2.0.4 | v2.0.5 | v2.1.0 |
|---------|--------|--------|--------|--------|--------|
| Tab selection | `:has-text >> nth=0` (broken) | `:text-is()` + fallbacks | — | — | — |
| Tool matrix | TODO placeholder | Confirmed matrix | — | — | — |
| `think_mode` aliases | Incomplete, "search" wrong | All aliases correct | — | — | — |
| Send button selector | `_7436101` (stale hash) | — | Stable `ds-button--` classes | — | — |
| CONTINUE mode response | Returns old response | — | Anchored to post-send count | — | — |
| CONTINUE mode DOM calls | Mode/tool selectors called | — | Skipped (controls hidden) | — | — |
| `/v1/models` `owned_by` | `"deepseek"` | — | — | `"PAF-ai"` | — |
| `/v1/models` fields | Full OpenAI schema | — | — | Minimal `id/object/owned_by` | — |
| `/v1/models` data source | `MODEL_ALIASES.keys()` | — | — | `list_all_accounts()` | — |
| Session TTL | ❌ | — | — | — | ✅ 3600s, configurable |
| Session disk restore | ❌ | — | — | — | ✅ `load_from_disk()` |
| Account pinning (CONTINUE) | ❌ | — | — | — | ✅ `Session.account` |
| Turn 2 tool-result | ❌ | — | — | — | ✅ `scrape_with_tool_result()` |
| CONTINUE bug: prompt ke new chat | ❌ Bug | — | — | — | ✅ Fixed (5 root causes) |
| `_ensure_page_ready()` URL check | ❌ | — | — | — | ✅ Sanity check + fallback |
| `is_session_expired()` false positive | ❌ `:has-text("Log in")` | — | — | — | ✅ `input[type=password]` visible |
| `_rotate_account()` reset flag | ❌ | — | — | — | ✅ Reset `_conversation_started` |
| Background cleanup loop | ❌ | — | — | — | ✅ Setiap 60 detik |
| Skip-goto optimisation | ❌ | — | — | — | ✅ URL compare sebelum goto |
| `pool.release(reset=)` | ❌ | — | — | — | ✅ CONTINUE jaga page hidup |
| CLI `--mode continue` | ❌ Selalu buka chat baru | — | — | — | ✅ Simpan + muat URL |
| `examples/chat.py` | ❌ | — | — | — | ✅ Interactive API client |
| `newpublic_BETA.py` | ✅ Ada | — | — | — | 🗑️ Dihapus |

---

## [2.0.2] and earlier

See git history (`git log --oneline`) for changes prior to v2.0.3.
