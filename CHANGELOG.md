# Changelog — PAF-ModelDeepSeek

All notable changes to this project are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [2.1.3] 2026-06-27 — Fix CONTINUE mode blank response: virtual list, selector, dan timing

Tiga bug berlapis yang menyebabkan `response_chars=0` pada semua CONTINUE mode
request meskipun DeepSeek sudah menampilkan response di browser.

---

### Bug Fixes

#### Bug #5 — `_is_stop_button_present` tidak pernah return `True` → Phase 1 selalu hangfire 15 detik (`scrapers/base_scraper.py`)

**Root cause:**

Dua selector teratas bergantung pada `:has(svg[class*='stop'])`:

```python
".ds-button--circle.ds-button--primary:not(.ds-button--disabled):has(svg[class*='stop'])",
".ds-button--circle:has(svg[class*='stop'])",
```

`:has()` tidak didukung oleh `querySelectorAll` dan DeepSeek menggunakan icon
library dengan class minified (`_abc123`, bukan `stop`). Selector ini tidak
pernah match → `stop_seen` selalu `False` → scraper menunggu penuh **15 detik
grace window** di setiap request sebelum masuk Phase 2.

**Fix (`scrapers/base_scraper.py`):**

`_is_stop_button_present()` diimplementasi ulang menggunakan `page.evaluate()`
(satu JS round-trip, identik dengan pendekatan PAF-ModelQwen) dengan strategi
selector berlapis di dalam JS:

1. `.ds-button--circle.ds-button--primary:not(.ds-button--disabled)` + cek
   `offsetParent !== null` — tanpa `:has()`, manual visibility check
2. `[aria-label*="stop"]` — case-insensitive, fallback berbasis atribut
3. `[class*="stop-btn"]`, `[class*="stopBtn"]` — fragment class
4. `[class*="streaming"]`, `[class*="typing"]`, `.result-streaming` — indikator
   streaming sebagai fallback terakhir

Menggantikan loop 8 Playwright locator (8 CDP round-trips) dengan 1 JS call.

---

#### Bug #6 — Selector `ds-markdown` match elemen thinking, bukan response (`config.py`)

**Root cause:**

Semua selector `assistant_message` dan `response_container` hanya menarget
`div.ds-markdown`. DeepSeek menggunakan class yang sama untuk **dua jenis
elemen berbeda**:

| Elemen | Class aktual | Isi |
|--------|-------------|-----|
| Reasoning / chain-of-thought | `ds-markdown` (saja) | Internal thinking process |
| Response user-facing | `ds-markdown ds-assistant-message-main-content` | Teks yang harus dikembalikan |

Akibatnya `_read_latest_response()` kadang membaca thinking block DeepSeek,
bukan response sesungguhnya. Atau count elemen jadi inflated sehingga
`skip_count` salah.

**Dikonfirmasi dari DOM debug log (27 Juni 2026):**

```
cls='ds-markdown' role='' | 'The user said "curhat mungkin?" ...'  ← thinking block
cls='ds-markdown ds-assistant-message-main-content' | 'Siap, silakan curhat aja...'  ← response asli
```

**Fix (`config.py`):**

Selector diperbarui dengan `ds-assistant-message-main-content` sebagai
discriminator utama:

```python
"assistant_message": [
    "div.ds-assistant-message-main-content",
    "div.ds-markdown.ds-assistant-message-main-content",
    "div[class*='ds-assistant-message-main-content']",
    "div.ds-markdown",              # fallback lebar
    "div[class*='ds-markdown']",
],
"response_container": [
    "div.ds-assistant-message-main-content",
    "div.ds-markdown.ds-assistant-message-main-content",
    "div[class*='ds-assistant-message-main-content']",
    "div.ds-markdown",
    "div[class*='ds-markdown']",
],
```

Semua selector `:last-of-type` dihapus — lihat Bug #7.

---

#### Bug #7 — Virtual list + `:last-of-type` + `skip_count` count-based → response tidak pernah terbaca (`scrapers/base_scraper.py`)

**Root cause (tiga komponen):**

**7a — `:last-of-type` selalu return `count=1`:**

CSS pseudo-class `:last-of-type` diproses oleh browser sebelum Playwright
memfilter — browser hanya mengembalikan satu elemen (yang terakhir). Playwright
`locator.count()` pada selector `:last-of-type` selalu = `1` regardless berapa
banyak elemen yang sebenarnya ada di DOM.

Di CONTINUE mode: `initial_response_count = 1` (baseline), saat polling
`count = 1` juga → `count > skip_count` = `1 > 1` = `False` → tidak pernah
membaca apapun → timeout 300 detik.

**7b — DeepSeek Virtual List:**

DeepSeek merender percakapan menggunakan `ds-virtual-list` — hanya pesan yang
visible di **viewport** yang exist di DOM. Artinya:

- `locator.count()` berfluktuasi tergantung scroll position
- Baseline `count` yang diambil sebelum `send_prompt()` bisa berbeda dari count
  saat polling (virtual list bisa re-render subset berbeda)
- Pendekatan `skip_count` integer pada dasarnya tidak reliable untuk virtual list

**7c — `count` baseline tidak akurat dari `_count_response_elements()`:**

`_count_response_elements()` memanggil `locator.count()` dengan selector yang
mengandung `:last-of-type` → selalu return 1. Baseline salah sejak awal.

**Root cause flow:**
```
NEW mode:   skip_count=0,  count=1  → 1 > 0 → OK ✅
CONTINUE:   skip_count=1,  count=1  → 1 > 1 → False → tidak baca ❌
```

**Fix (`scrapers/base_scraper.py`):**

Strategi skip_count berbasis integer diganti sepenuhnya dengan **text anchor**:

1. **`_snapshot_baseline_text()`** (method baru) — dipanggil sebelum
   `send_prompt()`, menyimpan teks response terakhir yang visible ke
   `self._baseline_response_text` via `page.evaluate()`.

2. **`_read_latest_response()`** diimplementasi ulang dengan `page.evaluate()`:
   - Mencari elemen terakhir (`querySelectorAll` → `els[els.length - 1]`)
   - Jika `skip_count > 0` (CONTINUE mode): bandingkan teks dengan
     `_baseline_response_text`; jika sama → response baru belum muncul,
     return `""` → lanjut polling
   - Jika berbeda → response baru sudah ada, return teks tersebut

3. **`_count_response_elements()`** diimplementasi ulang dengan
   `page.evaluate()` (tidak pakai Playwright locator) agar count akurat
   terlepas dari virtual list viewport.

```python
# Sebelum (broken):
# 1. _count_response_elements() → locator(":last-of-type").count() → selalu 1
# 2. skip_count = 1
# 3. Polling: locator.count() = 1 → 1 > 1 = False → tidak baca → timeout

# Sesudah (fix):
# 1. _snapshot_baseline_text() → page.evaluate() → simpan teks response terakhir
# 2. Polling: page.evaluate() → ambil teks elemen terakhir
# 3. Jika teks == baseline → return "" (belum ada response baru)
# 4. Jika teks != baseline → return teks (response baru ✅)
```

**Fix tambahan — `wait_for_response()` timeout fallback:**

Phase 2 kini melacak `best_text` (teks non-empty terakhir yang berhasil dibaca).
Saat timeout, `best_text` dikembalikan alih-alih `last_text` yang bisa kosong.
Safety net jika response sudah ada tapi tidak sempat mencapai stability threshold.

**Fix tambahan — `scrape_with_tool_result()` (`scrapers/deepseek_scraper.py`):**

Sama dengan `scrape()`: tambah `await self._snapshot_baseline_text()` sebelum
`send_prompt()` untuk menjamin text anchor terset di Turn 2 juga.

---

### Performance

#### `poll_interval` dan `stability_polls` diturunkan (`config.py`)

Setelah stop button detection diperbaiki (Bug #5), polling lebih jarang
menghabiskan full grace window. Nilai disesuaikan agar lebih responsif:

| Parameter | Sebelum | Sesudah |
|-----------|---------|---------|
| `poll_interval` | 0.8s | 0.5s |
| `stability_polls` | 4 | 2 |
| Phase 2 minimum | 3.2s | 1.0s |

---

### Diagnostics

#### `_dump_dom_debug()` — DOM introspection saat timeout (`scrapers/base_scraper.py`)

Method baru dipanggil otomatis saat `wait_for_response` timeout. Mengeluarkan
log `WARNING` berisi class, id, data-role, dan preview teks semua `div` dengan
konten bermakna (20–500 karakter). Digunakan untuk mengidentifikasi selector
yang tepat tanpa perlu buka DevTools secara manual.

Berguna untuk mendeteksi selector drift saat DeepSeek melakukan build update.

#### Timing log di `_read_latest_response()` (`scrapers/base_scraper.py`)

`log.debug` ditambahkan untuk setiap selector yang dicoba, menampilkan:
`sel`, `base_sel`, `count`, `skip_count`. Aktif pada log level `DEBUG`.

---

### Timing Log (dari sesi sebelumnya, v2.1.2)

Catatan: timing log di bawah ini diimplementasi pada sesi v2.1.2 (dalam sesi
yang sama dengan rilis ini) sebelum bug-bug di atas ditemukan:

- **`public.py` — `_handle_task()`**: log `TASK RECEIVED` (prompt preview,
  mode, session) dan `TASK DONE` / `TASK FAILED` / `TASK ERROR` dengan
  durasi `total` dan `scrape` dalam detik.
- **`browser_pool.py` — `run_task()` dan `run_task_with_tool_result()`**:
  log `run_task done` dengan breakdown `acquire` (waktu tunggu slot) vs
  `scrape` (waktu eksekusi browser) vs `total`.
- **`browser_pool.py`**: tambah `import time`.
- **`_execute_task()` di `public.py`**: `log.debug` dengan `elapsed` setelah
  `pool.run_task()` selesai.

---

### Files Changed

| File | Perubahan |
|------|-----------|
| `config.py` | `assistant_message` + `response_container`: selector diperbarui ke `ds-assistant-message-main-content`; hapus semua `:last-of-type`; turunkan `poll_interval` 0.8→0.5, `stability_polls` 4→2 |
| `scrapers/base_scraper.py` | `_is_stop_button_present()`: implementasi ulang dengan `page.evaluate()`; `_count_response_elements()`: implementasi ulang dengan JS; `_snapshot_baseline_text()`: method baru; `_read_latest_response()`: implementasi ulang dengan JS + text anchor; `wait_for_response()`: tambah `best_text` fallback + panggil `_dump_dom_debug()` saat timeout; `_dump_dom_debug()`: method baru |
| `scrapers/deepseek_scraper.py` | `scrape_with_tool_result()`: tambah `_snapshot_baseline_text()` sebelum send |
| `public.py` | `_handle_task()`: timing log `TASK RECEIVED/DONE/FAILED/ERROR`; `_execute_task()`: timing log elapsed |
| `browser_pool.py` | `import time`; `run_task()` + `run_task_with_tool_result()`: timing log `acquire/scrape/total` |
| `CHANGELOG.md` | Entri ini |

---

### Version comparison (penambahan dari v2.1.2)

| Feature | v2.1.2 | v2.1.3 |
|---------|--------|--------|
| Stop button detection | Selector `:has(svg[class*='stop'])` (tidak pernah match) | JS evaluate, tanpa `:has()`, + streaming indicator fallback |
| Phase 1 grace window hangfire | ❌ Selalu 15 detik per request | ✅ Stop button terdeteksi → Phase 1 aktif |
| Response selector | `div.ds-markdown` (match thinking + response) | `div.ds-assistant-message-main-content` (response saja) |
| CONTINUE response blank | ❌ `response_chars=0` → timeout 300s | ✅ Text anchor — baca response baru segera |
| `:last-of-type` count bug | ❌ `count` selalu 1 | ✅ Dihapus, diganti JS evaluate |
| Virtual list compatibility | ❌ `skip_count` integer tidak reliable | ✅ Text anchor immune terhadap virtual list |
| `_read_latest_response` | Playwright locator loop | `page.evaluate()` single round-trip |
| `_count_response_elements` | Playwright locator + `:last-of-type` | `page.evaluate()` |
| Timeout fallback | Return `last_text` (bisa kosong) | Return `best_text` (teks non-empty terakhir) |
| DOM debug on timeout | ❌ | ✅ `_dump_dom_debug()` otomatis |
| Timing log worker | ❌ | ✅ `TASK RECEIVED/DONE/FAILED/ERROR` + durasi |
| Timing log pool | ❌ | ✅ `acquire/scrape/total` per task |
| `poll_interval` | 0.8s | 0.5s |
| `stability_polls` | 4 | 2 |


---


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