# Changelog — PAF-ModelDeepSeek

All notable changes to this project are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [2.1.2] 2026-06-27 — Fix wait_for_response timeout: selector fallback, stop-button signal, SPA race condition

Tiga bug yang menyebabkan `wait_for_response timed out after 300s` meski response
DeepSeek sudah selesai — response tidak pernah dikirim ke client.

---

### Bug Fixes

#### Bug #1 — `assistant_message` selector tidak match DOM setelah build update (`config.py`)

**Root cause:**

`assistant_message` hanya memiliki dua selector, keduanya bergantung penuh pada
class `ds-markdown`:

```python
"assistant_message": [
    "div.ds-markdown:last-of-type",
    "div[class*='ds-markdown']:last-of-type",
]
```

DeepSeek adalah React SPA dengan class name yang di-minify dan **berubah antara
build**. Jika `ds-markdown` berganti nama, `_read_latest_response()` selalu
return `""` → polling terus sampai batas 300 detik.

**Fix (`config.py`):**

Ditambahkan 5 fallback selector baru, diurutkan dari paling spesifik ke paling
longgar. Scraper mencoba tiap selector berurutan — jika satu gagal karena class
drift, selector berikutnya mengambil alih:

| # | Selector | Strategi |
|---|----------|----------|
| 1 | `div.ds-markdown:last-of-type` | Original — paling presisi |
| 2 | `div[class*='ds-markdown']:last-of-type` | Partial-class variant |
| 3 | `[data-role='assistant'] div.ds-markdown` | Anchor pada atribut role (stabil) |
| 4 | `[data-role='assistant']:last-of-type` | Role-only — tanpa dependency class |
| 5 | `div[class*='markdown']:last-of-type` | Fragment paling longgar |
| 6 | `.dad65929:last-of-type` | Verified minified wrapper (Juni 2026) |
| 7 | `div[class*='_message']:last-of-type` | Generic message bubble fallback |

Selector `stop_button` juga diperluas dengan selector struktural berbasis
`ds-button--circle` yang tidak bergantung pada minified hash:

```python
# Baru — structural selector (tidak pakai hash minified):
".ds-button--circle.ds-button--primary:not(.ds-button--disabled):has(svg[class*='stop'])",
".ds-button--circle:has(svg[class*='stop'])",
# Legacy aria/class fallbacks tetap dipertahankan di bawah
```

---

#### Bug #2 — `stability_polls` direset oleh post-stream re-render (`scrapers/base_scraper.py`)

**Root cause:**

`wait_for_response()` menganggap response selesai hanya jika teks **sama persis**
selama `stability_polls=4` poll berturut-turut (4 × 0.8s = 3.2 detik):

```python
if text and text == last_text:
    stable_count += 1
    if stable_count >= stability_polls:
        return text
else:
    stable_count = 0   # ← reset setiap teks berubah
    last_text = text
```

DeepSeek melakukan **re-render post-stream** — syntax highlighting dan math
rendering dieksekusi setelah streaming selesai, mengubah teks secara minor.
Setiap re-render mereset `stable_count` ke 0 → loop berlanjut hingga timeout.

**Fix (`scrapers/base_scraper.py`):**

`wait_for_response()` diganti dengan **two-phase strategy**:

**Phase 1 — Stop button gone (primary signal):**

Method baru `_is_stop_button_present()` mengecek apakah tombol stop-generation
masih ada di DOM. Tombol ini muncul saat DeepSeek mulai streaming dan **dihapus
dari DOM saat generation selesai** — jauh lebih reliable daripada stabilitas
teks karena tidak terpengaruh re-render.

```
stop button muncul  →  streaming dimulai
stop button hilang  →  generation selesai  ✅
```

**Grace window** (10% dari timeout, max 15 detik): jika stop button tidak
terdeteksi dalam window ini (kemungkinan selector drift), Phase 1 dilewati dan
langsung masuk Phase 2 — tidak hang selamanya.

**Phase 2 — Stable text (konfirmasi / fallback):**

Setelah stop button hilang (atau grace window habis), stability polling tetap
berjalan sebagai konfirmasi akhir. Sekarang hanya perlu menunggu post-render
kecil yang terjadi **setelah** streaming confirmed selesai — bukan seluruh durasi
streaming.

```python
# Flow baru (ringkasan):
# 1. Tunggu stop button muncul (grace window)
# 2. Tunggu stop button hilang  → generation selesai
# 3. Short settle sleep (0.5s) untuk post-render mutations
# 4. Stability polling sebagai konfirmasi akhir
```

Method baru yang ditambahkan:

```python
async def _is_stop_button_present(self) -> bool:
    """True jika stop-generation button masih visible di DOM."""
```

---

#### Bug #3 — Race condition: `_count_response_elements` dipanggil sebelum SPA hydrate (`browser_pool.py`)

**Root cause:**

Di `run_task()` (CONTINUE mode), urutan eksekusi sebelumnya:

```
page.goto(conversation_url, wait_until="domcontentloaded")
asyncio.sleep(2)          ← blind sleep, tidak deterministik
_conversation_started = True
↓
scrape() dipanggil
  └── _count_response_elements()  ← snapshot baseline di sini
```

Setelah `domcontentloaded`, React SPA DeepSeek masih perlu **hydrate dan render
ulang message history** yang ada. `asyncio.sleep(2)` tidak menjamin hydration
selesai — pada koneksi lambat atau CPU tinggi, 2 detik tidak cukup.

Jika `_count_response_elements()` dipanggil sebelum hydration selesai, ia
mengembalikan `0` (atau angka yang lebih kecil dari jumlah response aktual).
Akibatnya `skip_count` salah:

- Terlalu kecil → `_read_latest_response` membaca response lama yang sudah
  stabil → `wait_for_response` return seketika dengan teks salah
- Atau DOM belum ada sama sekali → return `""` → timeout 300 detik

Bug ini terjadi di `run_task()` **dan** `run_task_with_tool_result()`.

**Fix (`browser_pool.py`):**

`asyncio.sleep(2)` diganti dengan method baru `_wait_for_spa_ready()` yang
menunggu secara **deterministik** sampai SPA benar-benar siap:

```python
async def _wait_for_spa_ready(self, scraper, timeout_ms=10_000) -> None:
    """
    Tunggu chat input textarea attached ke DOM.
    React SPA DeepSeek tidak bisa merender chat input sebelum
    message history terhidrate — ini menjamin DOM lengkap.
    """
    for sel in chat_input_selectors:
        await scraper.page.locator(sel).first.wait_for(
            state="attached", timeout=timeout_ms
        )
        return
    log.warning("chat input not found — proceeding anyway")
```

`_wait_for_spa_ready()` diterapkan di dua tempat:
- `run_task()` — untuk semua CONTINUE request biasa
- `run_task_with_tool_result()` — untuk Turn 2 tool-result injection

---

### Files Changed

| File | Perubahan |
|------|-----------|
| `config.py` | `assistant_message`: 5 fallback selector baru; `stop_button`: selector struktural ditambah |
| `scrapers/base_scraper.py` | `_is_stop_button_present()` baru; `wait_for_response()` two-phase (stop-button + stability); docstring diperbarui |
| `browser_pool.py` | `_wait_for_spa_ready()` baru; `run_task()` + `run_task_with_tool_result()` ganti `sleep(2)` dengan `_wait_for_spa_ready()` |
| `CHANGELOG.md` | Entri ini |

### Version comparison (penambahan dari v2.1.1)

| Feature | v2.1.1 | v2.1.2 |
|---------|--------|--------|
| `assistant_message` selector | 2 selector (class-only) | 7 selector (class + role + structural) |
| `stop_button` selector | 3 selector (aria/class) | 8 selector (structural + aria + class) |
| `wait_for_response` completion signal | Stability polling saja | Stop-button gone (primary) + stability (fallback) |
| Post-stream re-render reset | ❌ Reset `stable_count` | ✅ Tidak terpengaruh (Phase 1 tidak pakai text diff) |
| SPA hydration setelah `goto()` | `sleep(2)` (non-deterministik) | `_wait_for_spa_ready()` (deterministik) |
| `skip_count` baseline akurasi | Bisa salah jika SPA belum hydrate | ✅ Selalu benar — DOM sudah lengkap |

---

## [2.1.1] 2026-06-27 — Optimisasi performa: input fill & auth cache

Dua optimisasi performa ringan yang tidak mengubah perilaku fungsional.

---

### Performance

#### `send_prompt()` — ganti `type()` dengan `fill()` (`scrapers/deepseek_scraper.py`)

`input_loc.type(prompt, delay=15)` mengetik karakter satu per satu dengan jeda
15ms per karakter untuk terlihat "human". Untuk prompt 200 karakter, ini
memakan **~3 detik** hanya pada tahap input.

Diganti dengan `input_loc.fill(prompt)` yang men-set value sekaligus, diikuti
satu `asyncio.sleep` pendek (`fill_settle_ms`) agar React SPA sempat meregistrasi
input event sebelum tombol send diklik.

```python
# Sebelum (~3s untuk prompt 200 char):
await input_loc.fill("")
await input_loc.type(prompt, delay=15)
await asyncio.sleep(0.4)

# Sesudah (~0.12s flat):
await input_loc.fill(prompt)
await asyncio.sleep(BROWSER_CONFIG.get("fill_settle_ms", 120) / 1000)
```

Konfigurasi `fill_settle_ms` (default: `120`) tersedia di `BROWSER_CONFIG`
pada `config.py` untuk penyesuaian jika SPA butuh waktu lebih lama.

#### `ensure_authenticated()` — auth cache per-session (`scrapers/deepseek_scraper.py`)

Sebelumnya `ensure_authenticated()` selalu memanggil `is_session_expired()` di
setiap `scrape()` — artinya setiap request melakukan DOM query (cek URL, cek
`input[type="password"]`, cek `query_selector` chat input, bahkan `inner_text("body")`
sebagai last resort). Ini overhead yang sia-sia karena session hampir tidak pernah
expired di tengah-tengah penggunaan normal.

Kini `ensure_authenticated()` mempunyai fast path: jika `_authenticated=True`
**dan** browser masih berada di domain DeepSeek, langsung return `True` tanpa
menyentuh DOM sama sekali.

```python
# Fast path (hampir selalu diambil setelah request pertama):
if self._authenticated:
    if DEEPSEEK_CONFIG["base_url"] in self.page.url:
        return True   # skip seluruh DOM check
```

Cache ter-invalidasi otomatis pada tiga kondisi yang sudah ada:
- `restart_browser()` → `_authenticated = False`
- `_rotate_account()` → `_authenticated = False`
- URL drift ke luar domain DeepSeek → fallback ke full DOM check

---

### Config

#### `config.py` — `BROWSER_CONFIG`

- Tambah `fill_settle_ms: 120` — durasi sleep (ms) setelah `fill()` sebelum klik send.
- `type_delay_ms` dipertahankan sebagai referensi tapi tidak lagi dipakai oleh scraper.

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