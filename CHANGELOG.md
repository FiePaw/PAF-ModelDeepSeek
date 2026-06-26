# Changelog

All notable changes to PAF-ModelDeepSeek will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.2] - 2026-06-26

### Fixed
- **Browser cleanup method**: Fixed `browser_pool.py` calling incorrect method name when closing browser slots
  - Changed `slot.scraper.close()` to `slot.scraper.close_browser()`
  - Eliminates warning: `'DeepSeekScraper' object has no attribute 'close'`
  - Ensures proper browser cleanup when toggling headless mode
  - Prevents potential memory leaks from unclosed browser instances

### Impact
- `showheadless` command now works cleanly without warnings
- Browser lifecycle management improved
- Memory usage more stable during slot respawns

---

## [2.0.1] - 2026-06-26

### Fixed
- **CLI event loop threading**: Fixed interactive CLI commands failing with event loop error
  - Added `self._loop` reference to store main event loop
  - Capture event loop at worker startup with `asyncio.get_running_loop()`
  - Pass saved loop to `asyncio.run_coroutine_threadsafe()` in CLI thread
  - Resolves: `RuntimeWarning: coroutine '_handle_command' was never awaited`
  - Resolves: `Error: There is no current event loop in thread 'Thread-1'`

### Impact
- All CLI commands (`status`, `list accounts`, `showheadless`, `add account`, `quit`) now work correctly
- Background thread properly schedules async commands to main event loop
- No more runtime warnings during interactive worker management

---

## [2.0.0] - 2026-06-26

### Added - Worker Features (public.py)

#### Session Management
- **Session persistence**: Disk-based session storage in `data/sessions/`
  - `SessionStore` class with save/load/delete methods
  - JSON format with conversation URL, account, timestamps
  - Automatic session directory creation
  
- **CONTINUE mode**: Navigate to saved conversation URL
  - Retrieve session file and navigate to chat URL
  - Preserves full conversation context
  - Works with per-session locks to prevent collisions

- **Per-session locks**: Anti-collision protection
  - One request per session at a time
  - Prevents race conditions in CONTINUE mode
  - Automatic lock acquisition/release

#### Account Management
- **preferred_account routing**: Route to specific accounts with fallback
  - Request-level account preference
  - Automatic fallback to any available account if preferred is busy
  - Returns actual account used in response metadata

- **Runtime account addition**: Add accounts without restart
  - `add account NAME` CLI command
  - Automatic login via `ensure_authenticated()`
  - Immediate availability after successful login
  - Account list synced to VPS automatically

- **Per-account headless control**: Toggle browser visibility per account
  - `showheadless ACCOUNT` CLI command
  - Slot respawn with new headless mode
  - Independent control per account

#### Worker Management
- **Interactive CLI**: 5 REPL commands for runtime management
  - `list accounts` - Show all available accounts
  - `add account NAME` - Add new account with auto-login
  - `status` - Display pool status (idle/busy/dead slots)
  - `showheadless ACCOUNT` - Toggle browser visibility
  - `quit` - Graceful shutdown
  
- **Keepalive ping**: Prevent WebSocket disconnections
  - 30-second heartbeat to VPS
  - Maintains persistent connection
  - Automatic reconnect on failure

- **Account updates to VPS**: Runtime sync
  - Notify VPS when accounts change
  - Sent after pool initialization
  - Sent after `add account` command
  - Keeps VPS account list current

#### Error Handling
- **Error status mapping**: HTTP status codes
  - `429` - Rate limited by DeepSeek
  - `504` - Worker timeout (no response)
  - `401` - Authentication required
  - `500` - Browser/internal error
  - Propagated from worker to VPS to client

- **Stream rejection**: Explicit error for unsupported streaming
  - Returns `400` with clear error message
  - `"type": "invalid_request_error"`
  - `"code": "streaming_not_supported"`

#### Protocol Enhancements
- **Attachment support**: Base64 file uploads
  - Structure: `{"filename": "...", "data": "...", "mime_type": "..."}`
  - Passed directly to scraper
  - Supports images, documents, code files

- **Enhanced auto-reconnect**: Exponential backoff
  - Starts at 2 seconds, increases to 60 seconds
  - Automatic reconnection on disconnect
  - Maintains worker availability

---

### Added - VPS Features (vps_server.py)

#### API Features
- **CORS middleware**: Web frontend support
  - `allow_origins=["*"]`
  - `allow_methods=["*"]`
  - `allow_headers=["*"]`
  - Enables browser-based API calls

- **think_mode aliases**: Simplified API
  - `"instant"` → instant tab, no DeepThink
  - `"thinking"` / `"deep"` → expert tab + DeepThink
  - `"search"` → expert tab + search
  - `"vision"` → vision tab
  - Maps to internal `model_tab`, `deep_think`, `search` fields

- **Attachment structure**: Standardized format
  - `filename` + `data` + `mime_type`
  - Validated and forwarded to worker
  - Supports multiple attachments per request

#### Worker Management
- **VPS-generated worker IDs**: Collision-free IDs
  - Format: `worker-{hostname}-{counter:03d}`
  - Assigned by VPS on connection
  - No client-side ID generation
  - Guarantees uniqueness across reconnects

- **Worker wait timeout**: 60-second queue
  - Requests wait up to 60s for available worker
  - Returns `504` if timeout exceeded
  - Configurable via `WORKER_WAIT_TIMEOUT` env var

- **Account updates from worker**: Runtime sync
  - `update_accounts` WebSocket message
  - Updates VPS account registry
  - Reflects in `/v1/models` endpoint immediately
  - Triggered when worker adds accounts

#### Endpoints
- **Enhanced /v1/models**: Live account list
  - Returns all accounts from all connected workers
  - Real-time availability status
  - OpenAI-compatible format
  - Each account shown as a "model"

- **Worker stats endpoint**: Detailed monitoring
  - `/health` shows all workers, accounts, slots
  - Per-worker breakdown (total/busy slots)
  - Connection timestamps
  - Total system capacity

#### Response Enhancements
- **Response headers**: Tracking metadata
  - `X-Session-ID` - Session identifier if used
  - `X-Account-Name` - Which account processed request
  - `X-Conversation-URL` - DeepSeek chat URL for debugging
  - Available in all responses

- **x_meta block**: Rich metadata object
  ```json
  {
    "session_id": "...",
    "account_name": "...",
    "conversation_url": "...",
    "model_tab": "...",
    "deep_think": true/false,
    "search": true/false,
    "worker_id": "...",
    "processing_time_ms": 1234
  }
  ```

- **finish_reason field**: OpenAI compatibility
  - Always set to `"stop"`
  - Required by OpenAI SDK
  - Indicates completion type

#### Error Handling
- **reject_future() on error**: Fast-fail behavior
  - Immediately reject pending future on worker error
  - Prevents client waiting for timeout
  - Returns error to client faster
  - Improves error responsiveness

---

### Added - Browser Pool Features (browser_pool.py)

- **preferred_account routing**: Smart slot selection
  - Prioritize slots with preferred account
  - Fall back to any available slot if preferred busy
  - Returns actual account used
  - Enables account-level load distribution

- **conversation_url navigation**: CONTINUE support
  - Navigate to saved conversation URL before sending prompt
  - Preserves full conversation context
  - Integrated with session persistence
  - Automatic URL validation

- **Per-account headless control**: Fine-grained visibility
  - `_account_headless` dict tracks per-account settings
  - `set_account_headless()` method to toggle
  - Slot respawn with new headless mode
  - Useful for debugging specific accounts

---

### Changed

#### Architecture
- **Worker ID lifecycle**: VPS now assigns worker IDs
  - Workers no longer generate their own IDs
  - Eliminates potential collisions
  - Simplifies worker implementation

#### API Behavior
- **Streaming**: Now explicitly rejected with 400 error
  - Previously silently ignored
  - Clear error message: "Streaming is not supported"
  - Better developer experience

#### Error Propagation
- **Status codes**: More specific HTTP codes
  - Rate limits: 429 (was 500)
  - Timeouts: 504 (was 500)
  - Auth failures: 401 (was 500)
  - Better client-side error handling

---

### Fixed

#### Known Issues in v1.x
- No session persistence - conversations couldn't be continued
- No account routing - random account selection only
- No worker management CLI - required restarts for changes
- Generic 500 errors - hard to debug issues
- No CORS - web frontends couldn't call API
- Limited metadata - debugging was difficult
- No worker stats - capacity planning impossible

---

### Documentation

- **UPDATE_NOTES.md**: Complete feature documentation with examples
- **DEPLOYMENT_GUIDE.md**: Step-by-step deployment and testing guide
- **IMPLEMENTATION_SUMMARY.md**: Quick reference and architecture overview
- **test_updates.py**: Validation test suite for all 26 features
- **API_USAGE.md**: Complete API reference with examples
- **CHANGELOG.md**: This file

---

## [1.0.0] - 2024 (Baseline)

### Initial Release

#### Core Features
- Playwright-based browser automation for chat.deepseek.com
- WebSocket bridge between VPS and local workers
- OpenAI-compatible REST API (`/v1/chat/completions`)
- Multi-account support via browser profiles
- Persistent browser contexts (stay logged in)
- Browser pool with parallel slots
- Auto-reconnect on disconnect
- Basic error handling

#### Components
- `public.py` - Local worker with browser pool
- `vps_server.py` - FastAPI VPS bridge
- `browser_pool.py` - Browser slot management
- `scrapers/deepseek_scraper.py` - DeepSeek-specific automation
- `scrapers/base_scraper.py` - Generic scraper foundation

#### Limitations (addressed in v2.0.0)
- No session persistence
- No account routing
- No runtime management
- Basic error reporting (all 500s)
- No CORS support
- Minimal metadata
- No worker statistics
- Manual account management only

---

## Version Comparison

| Feature | v1.0.0 | v2.0.0 | v2.0.1 | v2.0.2 |
|---------|--------|--------|--------|--------|
| Session Persistence | ❌ | ✅ | ✅ | ✅ |
| Account Routing | ❌ | ✅ | ✅ | ✅ |
| Interactive CLI | ❌ | ✅ | ✅ | ✅ |
| CLI Works Correctly | N/A | ❌ | ✅ | ✅ |
| Browser Cleanup | ⚠️ | ⚠️ | ⚠️ | ✅ |
| HTTP Status Codes | ❌ | ✅ | ✅ | ✅ |
| CORS Support | ❌ | ✅ | ✅ | ✅ |
| Rich Metadata | ❌ | ✅ | ✅ | ✅ |
| Worker Stats | ❌ | ✅ | ✅ | ✅ |
| Runtime Account Add | ❌ | ✅ | ✅ | ✅ |
| Per-account Headless | ❌ | ✅ | ✅ | ✅ |

---

## Migration Guide

### From v1.0.0 to v2.0.2

#### Required Changes
1. **Replace 3 core files**:
   - `public.py` → v2 (CLI fix applied)
   - `vps_server.py` → v1 (no changes needed)
   - `browser_pool.py` → v2 (close method fix applied)

2. **Create session directory**:
   ```bash
   mkdir -p data/sessions
   ```

3. **Optional: Set VPS environment variables**:
   ```bash
   export PAF_TOKEN="your-token"
   export WORKER_WAIT_TIMEOUT=60
   ```

#### New Features Available
- Use `session_id` and `mode` for conversation continuity
- Use `preferred_account` for account routing
- Use `think_mode` for simplified mode selection
- Use CLI commands for runtime management
- Check `/health` for detailed worker stats
- Check `x_meta` in responses for debugging info

#### Breaking Changes
- **None**: v2.0.x is fully backward compatible with v1.0.0 API
- Old code continues to work without modifications
- New features are opt-in via new request parameters

#### Recommended Updates
1. Add error handling for new HTTP status codes (429, 504, 401)
2. Implement session persistence for multi-turn conversations
3. Use `preferred_account` for load distribution
4. Monitor `/health` endpoint for capacity planning
5. Use `x_meta` for debugging and logging

---

## Support

For issues or questions:
- Check the **DEPLOYMENT_GUIDE.md** for troubleshooting
- Review **API_USAGE.md** for API reference
- Consult **UPDATE_NOTES.md** for detailed feature docs
- See **HOTFIX_v2.0.1.md** and **HOTFIX_v2.0.2.md** for bug fixes

---

**Current Version**: 2.0.2  
**Release Date**: June 26, 2026  
**Status**: Production Ready ✅
