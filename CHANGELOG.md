# Changelog — PAF-ModelDeepSeek

All notable changes to this project are documented here.
Format: `[version] YYYY-MM-DD — summary`

---

## [2.5.0] 2026-06-30 — Qwen-aligned tool calling + repair parity + VPS tool forwarding

Rilis ini membawa tool calling PAF-ModelDeepSeek ke **paritas penuh** dengan
PAF-ModelQwen, sekaligus memperbaiki bug kritis di VPS yang menyebabkan
**infinite tool-call loop**.

### Bug Fixes

#### `PublicForward/ForVPS/vps_server.py` — Tool result forwarding (CRITICAL)
- **Root cause:** VPS hanya meneruskan `prompt = last_user_message()` ke worker.
  Saat CLI mengirim tool results (`role:"tool"`), VPS tetap mengirim prompt
  user asli — worker masuk ke `scrape()` (bukan `scrape_with_tool_result()`)
  → prompt dibungkus ulang `[SYSTEM CONTEXT]/[USER REQUEST]` → DeepSeek melihat
  permintaan yang sama → minta tool lagi → **infinite loop**.
- **Fix:** VPS sekarang mengekstrak `tool_messages` dari `messages` array dan
  meneruskan `tool_messages` + full `messages` array ke worker via dispatch.
  Worker mendeteksi `tool_msgs is not None` → route ke
  `scrape_with_tool_result()`.
- **ChatMessage model** ditambah field `tool_calls` dan `tool_call_id`
  (parity dengan Qwen) agar Pydantic tidak menolak message role:tool/assistant.

#### `scrapers/deepseek_scraper.py` — scrape_with_tool_result() missing wait_for_response
- **Root cause:** `send_prompt()` mengembalikan CSS selector string, bukan
  response text. `scrape_with_tool_result()` langsung memvalidasi selector
  string sebagai JSON → `Expecting value: line 1 column 1 (char 0)`.
- **Fix:** Tambah `_capture_pre_send_text()` + `wait_for_response()` setelah
  `send_prompt()`, sama dengan pattern di `scrape()`.

### Improvements (Qwen parity)

#### `scrapers/base_scraper.py` — Repair methods upgraded to Qwen versions
- **`_repair_unescaped_quotes`**: Ganti regex sederhana dengan content-block-based
  escaping — cari blok `"content":"..."`, tentukan batas akhir field, escape ulang
  semua quote/backslash/newline di dalamnya. Return `None` jika tidak applicable.
- **`_repair_tool_calls_arguments`**: Ganti regex dengan state-machine parser —
  parse karakter-per-karakter di dalam blok arguments, deteksi inner quote vs
  closing quote berdasarkan karakter berikutnya (`:`, `,`, `}`, `]`).

#### `scrapers/deepseek_scraper.py` — Tool result format (Qwen pattern)
- **`_build_tool_result_prompt()`** (NEW): Format `[TOOL RESULT]` + `[USER REQUEST]`
  dengan `{"continue":true,"model":"..."}`. Tanpa `[SYSTEM CONTEXT]` reminder.
- **`scrape_with_tool_result()`** (rewritten): `_capture_pre_send_text()` →
  `send_prompt()` → `wait_for_response()` → `_validate_deepseek_json_response()`.
  No corrective loop (Qwen pattern).
- **`_validate_deepseek_json_response()`**: Handle `None` return dari repair methods.

### Files Modified

| File | Changes |
|------|---------|
| `scrapers/base_scraper.py` | `_repair_unescaped_quotes` + `_repair_tool_calls_arguments` → Qwen versions |
| `scrapers/deepseek_scraper.py` | `_build_tool_result_prompt` (new) + `scrape_with_tool_result` (rewritten) + `_validate` fix |
| `PublicForward/ForVPS/vps_server.py` | `ChatMessage` model + tool_messages extraction + full messages forwarding |
| `API_USAGE.md` | Updated tool calling docs + internal architecture |
| `CHANGELOG.md` | This entry |

---

## [2.2.2] 2026-06-28 — Critical bug fix: virtual-scroll breaks count-based detection (CONTINUE #3+)

Rilis ini memperbaiki **Bug #9** — bug KRITIS yang tersisa dari sesi sebelumnya:
CONTINUE ke-3 dan seterusnya selalu timeout (response_chars=0) karena DeepSeek
menggunakan virtual scrolling yang menyebabkan element count tidak pernah berubah.
Sekaligus memperbaiki `_wait_for_spa_ready` yang selalu timeout 10s pada Skip goto().

### Bug Fixes

#### `scrapers/base_scraper.py` — Virtual scrolling breaks count-based detection (CRITICAL)
- **Root cause:** DeepSeek menggunakan `ds-virtual-list`. Elemen di luar viewport
  **dihapus dari DOM**. Setelah beberapa exchange, saat response baru muncul di
  bawah, satu elemen lama dihapus dari atas → total count **tetap sama** →
  `count > baseline` selalu False → timeout 300 s.
- **Fix:** Ganti count-based detection dengan **text-change detection**:
  1. `_get_last_response_text()` — baca teks elemen terakhir dari
     `.ds-virtual-list-visible-items div.ds-markdown` (virtual-scroll aware).
  2. `_capture_pre_send_text()` — snapshot teks sebelum `send_prompt()`.
  3. `wait_for_response()` dirombak: bandingkan teks saat ini dengan
     `pre_send_text`. Deteksi response baru saat teks BERUBAH, bukan saat
     count bertambah. Stability check tetap ada (teks harus stabil N polls).
  4. `_count_response_elements()` dipertahankan untuk diagnostic/backward-compat.
  5. `_virtual_list_selectors()` hook baru — subclass bisa override.
- **Immune terhadap:** virtual-scroll recycling, `:last-of-type` mismatch,
  DOM restructuring, fresh page (pre_send_text="" → response apapun terdeteksi).
- **Semua call site** di `scrape()` dan `scrape_with_tool_result()` diupdate
  untuk capture `pre_send_text` dan pass ke `wait_for_response()`.

#### `config.py` — Tambah selector `virtual_list_response`
- Tambah key `virtual_list_response` di `selectors` dengan selector scoped ke
  `.ds-virtual-list-visible-items div.ds-markdown` (primary) dan fallback
  unscoped untuk percakapan pendek di mana virtual list belum aktif.

#### `browser_pool.py` — `_wait_for_spa_ready` selalu timeout 10s
- **Root cause:** Chat input selector tidak pernah attach setelah virtual list
  aktif → `_wait_for_spa_ready` SELALU timeout ~10s pada setiap Skip goto() call.
- **Fix (Opsi A):** Jika chat input tidak ditemukan dalam `timeout_ms` DAN
  `conversation_url` tersedia → force `page.goto(conversation_url)` untuk
  reset DOM ke known-good state, lalu coba lagi. Eliminasi 10 s waste dan
  memastikan DOM fully rendered sebelum `_capture_pre_send_text()`.
- Semua 4 call site `_wait_for_spa_ready()` di `run_task()` dan
  `run_task_with_tool_result()` diupdate untuk pass `conversation_url`.

---



Rilis ini memperbaiki **8 bug** yang ditemukan setelah optimasi v2.2.0: satu bug
kritis pada scraper (mode=new timeout 300s karena response baseline salah), satu
bug kritis pada response detection (CONTINUE mode ke-2+ timeout karena selector-
baseline mismatch), empat bug pada pipeline session CONTINUE, dan dua bug pada
browser pool navigation setelah worker restart.
Ditambahkan juga `chatCLI.py` — interactive CLI untuk testing API.

### Bug Fixes

#### `scrapers/base_scraper.py` — Selector-baseline mismatch on CONTINUE (CRITICAL)
- **Root cause:** `_count_response_elements()` mengembalikan satu angka (int)
  dari selector PERTAMA (`div.ds-markdown:last-of-type`). Angka ini dipakai
  sebagai `skip_count` universal untuk SEMUA selector di `_read_latest_response()`.
  Karena `:last-of-type` mencocokkan satu elemen per parent-wrapper, pada DOM
  DeepSeek setiap response adalah anak tunggal → `:last-of-type` mencocokkan
  **semua** response → count-nya SAMA dengan `div.ds-markdown` total.
  `count > skip_count` menjadi `N > N` = **False** untuk semua selector →
  response baru tidak pernah terdeteksi → **timeout 300 s** (response_chars=0).
- **Kenapa first CONTINUE berhasil:** Setelah navigasi `page.goto()`, halaman
  baru ter-load dan response count masih rendah. Pada CONTINUE ke-2 (Skip goto,
  halaman sama), count meningkat dan `:last-of-type` count = total count →
  mismatch muncul.
- **Fix:** Tiga perubahan:
  1. `_count_response_elements()` sekarang mengembalikan `dict[str, int]` —
     per-selector baseline, bukan satu angka global.
  2. `_read_latest_response()` menerima `baselines: dict[str, int]` dan
     membandingkan setiap selector dengan baseline **-nya sendiri**.
  3. `wait_for_response()` menerima `dict | int` (backward compatible).
- Dipastikan `scrape_with_tool_result()` di `deepseek_scraper.py` juga kompatibel
  (sudah menggunakan `_count_response_elements()` yang sekarang return dict).

#### `browser_pool.py` — Skip goto() false positive after restart (CRITICAL)
- **Root cause:** Perbandingan URL menggunakan bidirectional substring check:
  ```python
  already_there = (
      conversation_url in current_url
      or current_url in conversation_url  # ← FALSE POSITIVE
  )
  ```
  Setelah worker restart, browser persistent profile biasanya membuka
  homepage (`https://chat.deepseek.com/`). Karena homepage URL adalah
  substring dari setiap conversation URL, check `current_url in
  conversation_url` selalu **True** → `Skip goto()` salah fire.
- **Dampak:** Browser tetap di homepage, bukan conversation page. Prompt
  dikirim ke chat kosong → response tanpa context percakapan sebelumnya.
  User melihat seolah session "tidak terestored" padahal session data
  tersimpan dengan benar di disk.
- **Fix:** Diganti dengan `_urls_match()` helper yang melakukan normalised
  path comparison via `urllib.parse`. Dua URL dianggap match hanya jika
  path-nya identik setelah stripping scheme/host/trailing-slash/query.
  Homepage (path kosong) **tidak pernah** match conversation URL.
- Diterapkan di **kedua** method: `run_task()` dan
  `run_task_with_tool_result()`.

#### `browser_pool.py` — Missing SPA ready check on Skip goto()
- **Root cause:** Saat `Skip goto()` fire, `_wait_for_spa_ready()` tidak
  dipanggil. Hanya dipanggil di branch `else` (saat `page.goto()` benar-
  benar dilakukan).
- **Dampak:** Setelah restart, meskipun URL match benar (persistent profile
  membuka conversation URL yang tepat), DOM mungkin belum selesai hydrating.
  `_count_response_elements()` bisa return 0 → baseline salah →
  `wait_for_response()` membaca response lama sebagai response baru.
- **Fix:** `_wait_for_spa_ready()` sekarang dipanggil di **kedua** branch
  (Skip goto dan normal goto), memastikan SPA selalu ter-hydrate sebelum
  `scrape()` berjalan.

#### `scrapers/base_scraper.py` — Response baseline ordering fix (CRITICAL)
- **Root cause:** `scrape()` mengambil snapshot `initial_response_count` dari
  halaman **sebelum** `send_prompt()` dipanggil. Pada mode `new`,
  `send_prompt()` → `_ensure_page_ready("new")` → `_goto_new_chat()` navigasi
  ke halaman kosong. Namun snapshot sudah terlanjur diambil dari halaman lama
  (yang mungkin punya N response dari sesi sebelumnya).
- **Dampak:** `wait_for_response()` menunggu `count > N` pada halaman baru
  yang hanya punya 1 response → tidak pernah terpenuhi → **timeout 300s**.
- **Fix:** Untuk mode `new`, baseline di-hardcode ke `0` (halaman baru selalu
  kosong). Untuk mode `continue`, snapshot diambil sebelum `send_prompt()`
  (tidak ada navigasi pada continue).
- **Kenapa baru muncul:** Optimasi v2.2.0 menghilangkan banyak sleep/delay
  yang sebelumnya secara tidak sengaja menutupi race condition ini.

#### `chatCLI.py` — Auto-capture server-generated session_id (Bug #1)
- **Root cause:** Saat user mengirim pesan tanpa explicit `session_id`, VPS
  auto-generate `sess-xxxx` dan simpannya di worker. Namun chatCLI tidak
  membaca balik `session_id` dari `x_meta` response.
- **Dampak:** Setiap pesan menggunakan `session_id=None` → VPS selalu generate
  session baru → selalu `mode=new`, CONTINUE tidak pernah aktif.
- **Fix:** Setelah response sukses, chatCLI membaca `x_meta.session_id` dan
  menyimpannya di `state.session_id` untuk pesan berikutnya. Ditampilkan info
  `"Auto-captured session: sess-xxxx"`.

#### `chatCLI.py` — first_message_sent set before request (Bug #2)
- **Root cause:** `first_message_sent = True` di-set **sebelum** `client.chat()`
  dipanggil. Jika request pertama gagal (error/timeout), CLI mengira session
  sudah terkirim, padahal server belum menyimpan session.
- **Dampak:** Pesan kedua dikirim dengan `mode=continue` → server tidak punya
  session → silent fallback ke `mode=new`.
- **Fix:** `first_message_sent` hanya di-set **setelah** response sukses.
  Jika request pertama gagal, pesan berikutnya tetap `mode=new`.

#### `PublicForward/ForVPS/vps_server.py` — x_meta missing mode field (Bug #3)
- **Root cause:** `x_meta` dalam response tidak menyertakan field `mode`.
- **Dampak:** Client tidak bisa memverifikasi apakah server menghormati
  `mode=continue` atau diam-diam fallback ke `mode=new`.
- **Fix:** Ditambahkan `"mode"` (mode aktual yang dipakai worker) dan
  `"mode_fallback"` (boolean, `true` jika continue di-downgrade ke new)
  ke `x_meta`.

#### `public.py` — Silent fallback to NEW without notification (Bug #4)
- **Root cause:** Saat `session_store.get(session_id)` mengembalikan `None`
  (session expired, worker restart, atau worker berbeda), mode diam-diam
  diubah ke `"new"` tanpa indikasi dalam response.
- **Dampak:** Client tidak tahu bahwa context percakapan hilang dan dimulai
  dari awal.
- **Fix:** Ditambahkan flag `mode_fallback = True` dalam result ketika
  fallback terjadi. Flag ini di-propagate melalui result → VPS x_meta →
  client, sehingga client bisa reset state session dan re-create session.

### New Features

#### `chatCLI.py` — Interactive CLI test tool (NEW FILE)
- CLI interaktif untuk testing API endpoint `POST /v1/chat/completions`.
- Mendukung session management (`/new`, `/continue`, `/session`), mode
  selection (`/think`), account routing (`/account`), server health check
  (`/health`), dan conversation history (`/history`).
- **Auto mode transition:** Pesan pertama dalam session otomatis `mode=new`,
  pesan berikutnya otomatis `mode=continue`.
- **Auto session capture:** Jika user mengirim pesan tanpa session, CLI
  otomatis membaca `session_id` dari server response.
- **Mode fallback detection:** Mendeteksi dan menampilkan warning jika
  server men-downgrade `continue` ke `new`.
- Colored output dengan ANSI codes, `x_meta` display (toggle dengan
  `/meta`), dan `--base-url` / `--session` / `--think` CLI flags.
- Usage: `python chatCLI.py [--base-url URL] [--session ID] [--think MODE]`

### Documentation

#### `API_USAGE.md` — Updated response metadata documentation
- Documented `mode` dan `mode_fallback` fields baru di `x_meta`.
- Field name dikoreksi: `account_name` → `account`, `search` → `web_search`.
- Ditambahkan `response_time` (float, seconds) dan `timestamp` (unix).
- Ditambahkan section "Handling `mode_fallback`" dengan contoh response
  dan client action.
- Python client example di-update untuk handle `mode_fallback`.
- Version bump ke 2.2.1.

### Files Changed

| File | Change |
|------|--------|
| `browser_pool.py` | Fix Skip goto() false positive + add SPA ready check |
| `scrapers/base_scraper.py` | Fix response baseline ordering for mode=new |
| `chatCLI.py` | New file — interactive CLI test tool |
| `public.py` | Add `mode_fallback` flag on session fallback |
| `PublicForward/ForVPS/vps_server.py` | Add `mode` and `mode_fallback` to x_meta |
| `API_USAGE.md` | Updated response metadata docs |

### Impact
- **Skip goto() false positive dieliminasi** — setelah restart, browser
  sekarang selalu navigasi ke conversation URL yang benar. Homepage tidak
  lagi salah match sebagai conversation page.
- **mode=new timeout bug dieliminasi** — scraper sekarang selalu menggunakan
  baseline 0 untuk mode new, memastikan `wait_for_response()` langsung
  menangkap response baru.
- **Session CONTINUE sekarang reliable end-to-end** — auto-capture
  session_id, `first_message_sent` hanya di-set setelah sukses, mode
  fallback ter-notifikasi ke client, dan session benar-benar ter-restore
  setelah worker restart.
- Tidak ada breaking change pada API contract — field baru `mode` dan
  `mode_fallback` di `x_meta` bersifat additive (client lama mengabaikannya).

---

## [2.2.0] 2026-06-27 — Performance optimisation + per-stage timing instrumentation

Rilis ini fokus pada **kecepatan per-proses**. Sebelumnya satu proses bisa
memakan >30 detik dengan banyak waktu mati (dead time) yang tidak berasal dari
kecepatan model itu sendiri, melainkan dari timeout berlebih, jeda UI, dan
ekor stabilitas yang panjang. Selain optimasi, ditambahkan instrumentasi
timing sehingga bagian yang lambat bisa **diukur**, bukan ditebak.

Tidak ada perubahan perilaku fungsional (mode, tools, session, CONTINUE tetap
sama) — hanya penghematan latensi dan logging baru.

### Performance Optimisations

#### `scrapers/deepseek_scraper.py` — `_find_first()` two-phase lookup
- **Bug latensi tersembunyi diperbaiki.** Sebelumnya `_find_first` menunggu
  timeout PENUH untuk SETIAP selector dalam daftar. Daftar berisi N selector
  yang tidak ada di DOM = `timeout × N` (mis. 3 selector × 2500ms = **7.5s
  terbuang**).
- Sekarang dua fase: **(1)** cek keberadaan instan tanpa menunggu (kasus umum
  langsung kembali tanpa biaya timeout), **(2)** jika tidak ketemu, budget
  timeout **dibagi rata** lintas selector sehingga total tunggu tidak pernah
  melebihi `timeout_ms`.

#### `scrapers/deepseek_scraper.py` — `_select_model_tab()`
- Lewati seluruh pencarian + klik tab bila mode yang diminta == mode default.
  Chat baru selalu terbuka pada mode default, jadi tidak ada yang perlu diubah
  pada jalur "instant" yang paling umum. Timeout pencarian tab diturunkan ke
  `timeouts.element_find`.

#### `scrapers/deepseek_scraper.py` — `_set_toggle()`
- Timeout pencarian toggle DeepThink/Search diturunkan dari 2500ms ke
  `timeouts.element_find` (1500ms), memanfaatkan fast-path `_find_first`.

#### `scrapers/deepseek_scraper.py` — `_goto_new_chat()`
- Reset `_active_model_tab = None` saat membuka chat baru (chat baru me-reset
  mode pills ke default) agar keputusan `_select_model_tab()` tetap benar.

#### `config.py` — timeouts & typing
- `stability_polls`: 4 → **2** dan `poll_interval`: 0.8 → **0.5** → ekor
  stabilitas turun dari **3.2s → 1.0s** (~2.2s hemat per proses).
- `between_actions`: 0.4 → **0.2** (~1s hemat akumulatif per proses).
- `type_delay_ms`: 15 → **0** — key event tetap dikirim per karakter, tetapi
  tanpa delay buatan (prompt 200 karakter ≈ 3s hemat).
- Tambah kunci baru `timeouts.element_find` (1500ms) sebagai budget bersama
  pencarian elemen UI.

### New — Timing Instrumentation (INFO log)
- `scrapers/base_scraper.py` `scrape()` kini mencetak ringkasan per tahap:
  `[TIMING] auth=… send=… wait=… total=… (mode=…)`.
- `scrapers/deepseek_scraper.py` `send_prompt()` mencetak rincian sub-tahap:
  `[TIMING] send_prompt: nav=… controls=… type=… send=… (mode=…)`.
- Memudahkan menemukan bottleneck nyata tanpa menambah dependensi.

### Impact
- Estimasi penghematan overhead non-model: **~6–13 detik per proses** pada
  kasus umum (terutama dari perbaikan `_find_first`, skip tab default, ekor
  stabilitas, dan typing delay). Waktu generasi model tetap apa adanya.

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

See git history (`git log --oneline`) for changes prior to v2.0.3