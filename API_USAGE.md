# 📡 PAF-ModelDeepSeek API Usage Guide

## 🌐 Base URL

```
http://16.79.2.204:9000
```

All endpoints are accessible via this base URL. For production, use HTTPS with a reverse proxy.

---

## 🔑 Authentication

Currently, the API does not require authentication tokens in requests. Authentication is handled at the VPS level via the worker connection token.

---

## 📋 Available Endpoints

### 1. Health Check
**GET** `/health`

Check VPS server health and worker status.

**Request:**
```bash
curl http://16.79.2.204:9000/health
```

**Response:**
```json
{
  "status": "healthy",
  "workers": {
    "total_workers": 2,
    "workers": [
      {
        "worker_id": "worker-DESKTOP-ABC123-001",
        "hostname": "DESKTOP-ABC123",
        "accounts": ["account1", "account2"],
        "total_slots": 2,
        "busy_slots": 0,
        "connected_at": "2026-06-26T11:45:32Z"
      },
      {
        "worker_id": "worker-SERVER-XYZ789-001",
        "hostname": "SERVER-XYZ789",
        "accounts": ["account3", "account4", "account5"],
        "total_slots": 3,
        "busy_slots": 1,
        "connected_at": "2026-06-26T11:47:15Z"
      }
    ],
    "total_accounts": 7,
    "busy_slots": 1
  },
  "timestamp": "2026-06-26T12:30:45Z"
}
```

---

### 2. List Models (Available Accounts)
**GET** `/v1/models`

List all available DeepSeek accounts registered across all workers.

**Request:**
```bash
curl http://16.79.2.204:9000/v1/models
```

**Response:**
```json
{
  "object": "list",
  "data": [
    {"id": "account1", "object": "model", "owned_by": "PAF-ai"},
    {"id": "account2", "object": "model", "owned_by": "PAF-ai"}
  ]
}
```

**Notes:**
- Each entry represents a logged-in DeepSeek browser account registered to a worker
- The `id` field is the account name — use it with `preferred_account` to route requests to a specific account
- `owned_by` is always `"PAF-ai"`

---

### 3. Chat Completions (Main API)
**POST** `/v1/chat/completions`

Send a message to DeepSeek and get a response.

#### Basic Request

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "What is quantum computing?"}
    ]
  }'
```

#### Request Body Schema

```json
{
  "model": "deepseek-chat",          // Required: Always "deepseek-chat"
  "messages": [                       // Required: Array of message objects
    {
      "role": "user",                 // Required: "user", "assistant", "system", or "tool"
      "content": "Your message here"  // Required: Message content (string or null for tool_calls)
    }
  ],
  
  // Optional parameters:
  "preferred_account": "account2",    // Route to specific account (with fallback)
  "session_id": "my-session",         // Enable session persistence
  "mode": "new",                      // "new" or "continue" (requires session_id)
  "think_mode": "thinking",           // DeepSeek mode alias (see table below)
  "model_tab": "expert",              // Direct tab selection: "instant", "expert", or "vision"
  "deep_think": true,                 // Enable/disable DeepThink toggle
  "search": false,                    // Enable/disable search toggle
  "max_tokens": 2000,                 // Max response tokens (passed to model via [USER REQUEST])
  "tools": [                          // OpenAI-compatible function definitions (see Example 9)
    {
      "type": "function",
      "function": {
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
          "type": "object",
          "properties": {"path": {"type": "string"}},
          "required": ["path"]
        }
      }
    }
  ],
  "attachments": [                    // File uploads (Base64)
    {
      "filename": "data.csv",
      "data": "base64-encoded-content...",
      "mime_type": "text/csv"
    }
  ],
  "stream": false                     // Must be false (streaming not supported)
}
```

> **System messages:** Include `{"role": "system", "content": "..."}` in the
> `messages` array. The content is merged into the internal `[SYSTEM CONTEXT]`
> sent to DeepSeek alongside the JSON API instructions.
>
> **Tool calling:** When `tools` is provided, DeepSeek may respond with
> `finish_reason: "tool_calls"` instead of a normal answer. Send the tool
> result back in a follow-up `continue` request with a `{"role": "tool", ...}`
> message (see Example 9).

#### think_mode Aliases

Simplify mode selection with these aliases:

| think_mode | → Mode | → DeepThink | → Search | Notes |
|------------|--------|-------------|----------|-------|
| `"instant"` / `"fast"` | Instant | `false` | `false` | Default mode, fastest response |
| `"thinking"` / `"deep"` | Expert | `true` | `false` | Expert mode + DeepThink tool |
| `"expert"` / `"reasoning"` | Expert | `true` | `false` | Alias for `"thinking"` |
| `"search"` | Instant | `false` | `true` | Instant mode + Search tool |
| `"vision"` | Vision | `false` | `false` | Vision/OCR mode |

> **Important — Tool Availability per Mode:**
> DeepSeek enforces which tools are available depending on the active mode:
>
> | Mode | DeepThink | Search |
> |------|-----------|--------|
> | Instant | ✅ Available | ✅ Available |
> | Expert | ✅ Available | ❌ Not available (hidden in UI) |
> | Vision | ✅ Available | ❌ Not available (hidden in UI) |
>
> The scraper enforces this matrix automatically — requesting `search: true` on
> Expert or Vision mode is silently ignored (no error, no warning).
> 
> **Why `"search"` maps to Instant mode:** The Search tool is only available
> on Instant mode. Using `"search"` alias automatically selects Instant to
> ensure the Search tool can be activated.

**Example with think_mode:**
```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "Explain quantum entanglement"}],
    "think_mode": "thinking"
  }'
```

#### Response Format

The API is **OpenAI Chat Completions–compatible** (same envelope shape as
PAF-ModelQwen), so OpenAI SDKs and standard HTTP clients work unchanged.

```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1719374917,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Quantum computing is a type of computing that uses quantum-mechanical phenomena..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 148,
    "total_tokens": 160
  },
  "x_meta": {
    "session_id": "my-session",
    "mode": "new",
    "mode_fallback": false,
    "account": "account1",
    "conversation_url": "https://chat.deepseek.com/chat/abc123",
    "model_tab": "expert",
    "deep_think": true,
    "web_search": false,
    "response_time": 3.24,
    "timestamp": 1719374917.45,

    "model": "account1",
    "account_index": 0,
    "account_status": "ok",
    "account_file": "profiles/account1",
    "retry_count": 0,
    "response_time_ms": 3245,
    "think_mode": null
  }
}
```

> **Token usage is now accurate.** `usage` is computed by the worker using
> `tiktoken` (`cl100k_base` encoding) — the same accurate counting approach as
> PAF-ModelQwen — and only falls back to a `len/4` estimate if `tiktoken` is
> unavailable on the worker. Earlier builds returned `0` for all token fields.
>
> **`x_meta` is enriched.** In addition to the VPS-level fields (`session_id`,
> `mode`, `account`, `conversation_url`, timing), the worker's `x_metadata`
> block is folded in (`account_index`, `account_status`, `account_file`,
> `retry_count`, `response_time_ms`, `model_tab`, `deep_think`, `web_search`,
> `think_mode`). `think_mode` is always `null` for DeepSeek because it uses the
> Layer 1 mode pill (Instant/Expert/Vision) + Layer 2 toggles (DeepThink/Search)
> instead of Qwen's `think_mode` dropdown.

#### Response Headers

Additional metadata is available in response headers:

```http
X-Session-ID: my-session
X-Account-Name: account1
X-Conversation-URL: https://chat.deepseek.com/chat/abc123
```

---

## 🎯 Usage Examples

### Example 1: Simple Question

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ]
  }'
```

**Response:**
```json
{
  "id": "req-001",
  "object": "chat.completion",
  "created": 1719374917,
  "model": "deepseek-chat",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "2 + 2 equals 4."
    },
    "finish_reason": "stop"
  }],
  "x_meta": {
    "session_id": null,
    "account": "account1",
    "conversation_url": "https://chat.deepseek.com/chat/xyz",
    "model_tab": "instant",
    "deep_think": false
  }
}
```

---

### Example 2: Preferred Account Routing

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "Hello"}
    ],
    "preferred_account": "account2"
  }'
```

**Behavior:**
- Attempts to route to `account2`
- Falls back to any available account if `account2` is busy
- Response includes actual account used in `x_meta.account`

---

### Example 3: Session Persistence (NEW + CONTINUE)

#### Step 1: Create New Session

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "What is 5 + 3?"}
    ],
    "session_id": "math-session",
    "mode": "new"
  }'
```

**Response:**
```json
{
  "choices": [{
    "message": {
      "content": "5 + 3 equals 8."
    }
  }],
  "x_meta": {
    "session_id": "math-session",
    "conversation_url": "https://chat.deepseek.com/chat/abc123"
  }
}
```

#### Step 2: Continue Session

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "Multiply that by 2"}
    ],
    "session_id": "math-session",
    "mode": "continue"
  }'
```

**Response:**
```json
{
  "choices": [{
    "message": {
      "content": "8 multiplied by 2 equals 16."
    }
  }],
  "x_meta": {
    "session_id": "math-session",
    "conversation_url": "https://chat.deepseek.com/chat/abc123"
  }
}
```

**Note:** The worker navigates to the saved conversation URL and continues the context.

---

### Example 4: Deep Thinking Mode (Expert)

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "Explain the Riemann Hypothesis"}
    ],
    "think_mode": "thinking"
  }'
```

**Behavior:**
- Switches to **Expert** mode with **DeepThink** tool enabled
- DeepSeek will show its reasoning process
- Search tool is **not available** on Expert mode — silently ignored even if set
- Response includes full reasoning + answer

---

### Example 4b: Search Mode (Instant + Search)

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "user", "content": "What are today's latest AI news?"}
    ],
    "think_mode": "search"
  }'
```

**Behavior:**
- Uses **Instant** mode with **Search** tool enabled
- DeepSeek accesses the internet for up-to-date information
- Search tool is **only available** on Instant mode, so the alias auto-selects it

---

### Example 5: File Upload (Attachments)

```bash
# First, encode your file to base64
FILE_BASE64=$(base64 -w 0 data.csv)

curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"deepseek-chat\",
    \"messages\": [
      {\"role\": \"user\", \"content\": \"Analyze this CSV file\"}
    ],
    \"attachments\": [
      {
        \"filename\": \"data.csv\",
        \"data\": \"$FILE_BASE64\",
        \"mime_type\": \"text/csv\"
      }
    ]
  }"
```

**Supported MIME types:**
- `text/csv`, `text/plain`, `text/markdown`
- `application/pdf`, `application/json`
- `image/png`, `image/jpeg`, `image/gif`

---

### Example 6: Python Client

```python
import requests

BASE_URL = "http://16.79.2.204:9000"

def chat(message: str, session_id: str = None, mode: str = "new"):
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": message}]
    }
    
    if session_id:
        payload["session_id"] = session_id
        payload["mode"] = mode
    
    response = requests.post(
        f"{BASE_URL}/v1/chat/completions",
        json=payload
    )
    
    data = response.json()
    return data["choices"][0]["message"]["content"]

# Usage
print(chat("What is AI?"))

# With session
print(chat("What is 10 + 5?", session_id="calc", mode="new"))
print(chat("Multiply by 3", session_id="calc", mode="continue"))
```

---

### Example 7: OpenAI Python SDK Compatible

```python
# PAF-ModelDeepSeek is OpenAI-compatible!
from openai import OpenAI

client = OpenAI(
    base_url="http://16.79.2.204:9000/v1",
    api_key="not-needed"  # No auth required
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)
```

---

### Example 8: System Prompt

`system` role messages are honored — they are merged into the internal
`[SYSTEM CONTEXT]` sent to DeepSeek.

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "system", "content": "You are a terse assistant. Answer in one sentence."},
      {"role": "user", "content": "Explain black holes."}
    ]
  }'
```

---

### Example 9: Tool Calling (OpenAI-compatible Function Calling)

Provide a `tools` array. If DeepSeek decides it needs a function, the response
comes back with `finish_reason: "tool_calls"` and a `tool_calls` array. Execute
the function locally, then send the result back in a follow-up `continue`
request (using the `session_id` from Turn 1, with a `tool` role message).

**Turn 1 — request with tools:**
```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "session_id": "tool-demo",
    "mode": "new",
    "messages": [{"role": "user", "content": "Create a file test.py"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}
          },
          "required": ["path"]
        }
      }
    }]
  }'
```

**Turn 1 response (tool call requested):**
```json
{
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_a1b2",
        "type": "function",
        "function": {"name": "write_file", "arguments": {"path": "test.py", "content": "print('hi')"}}
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

**Turn 2 — send the tool result back:**
```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "session_id": "tool-demo",
    "mode": "continue",
    "messages": [
      {"role": "assistant", "content": null, "tool_calls": [{"id": "call_a1b2", "type": "function", "function": {"name": "write_file", "arguments": {"path": "test.py"}}}]},
      {"role": "tool", "tool_call_id": "call_a1b2", "name": "write_file", "content": "{\"success\": true}"}
    ]
  }'
```

DeepSeek then returns a normal `finish_reason: "stop"` answer incorporating the
tool result.

---

> ### 🧩 How it works internally (JSON API mode)
>
> Under the hood, every prompt is wrapped in a `[SYSTEM CONTEXT]` / `[USER
> REQUEST]` envelope instructing DeepSeek to reply with a single-line JSON
> envelope (`{"status":"success","choices":[...]}` or `{"status":"tool_calls",
> ...}`). The scraper parses that envelope and forwards OpenAI-compatible fields
> to you — so **the HTTP API you call is unchanged**; you always send and receive
> standard OpenAI-style payloads.
>
> **Turn 1 (scrape):** If DeepSeek replies with invalid JSON, the worker sends a
> corrective-feedback prompt in the same conversation (up to
> `max_corrective_retries` times) before retrying. This mirrors PAF-ModelQwen.
>
> **Turn 2 (scrape_with_tool_result):** Tool results are injected as:
> ```
> [TOOL RESULT]
> {"tool_call_id":"call_001","name":"write_file","result":{"success":true}}
>
> [USER REQUEST]
> {"continue":true,"model":"account1"}
> ```
> No corrective loop runs in Turn 2 — if the response is invalid JSON, the
> error is returned immediately (same as PAF-ModelQwen). The client should
> handle the error and decide whether to retry.
>
> **JSON repair:** Both `_repair_unescaped_quotes` (content-block-based escaping)
> and `_repair_tool_calls_arguments` (state-machine argument escaping) are
> applied before giving up, matching PAF-ModelQwen's repair strategy exactly.
>
> This mode can be disabled server-side via `JSON_API_CONFIG["enabled"] = False`.

---

## ⚠️ Error Responses

### 400 Bad Request
```json
{
  "error": {
    "message": "Streaming is not supported",
    "type": "invalid_request_error",
    "code": "streaming_not_supported"
  }
}
```

**Causes:**
- `stream: true` was set (not supported)
- Missing required fields (`model`, `messages`)
- Invalid JSON format

---

### 401 Unauthorized
```json
{
  "error": {
    "message": "Authentication required",
    "type": "authentication_error",
    "code": "invalid_auth"
  }
}
```

**Cause:** Worker authentication failed (internal, rare)

---

### 429 Too Many Requests
```json
{
  "error": {
    "message": "Account rate-limited by DeepSeek",
    "type": "rate_limit_error",
    "code": "rate_limit_exceeded"
  }
}
```

**Causes:**
- DeepSeek rate limit hit for the account
- Too many requests in short time

**Solution:** Wait a few minutes or use a different account via `preferred_account`

---

### 500 Internal Server Error
```json
{
  "error": {
    "message": "Browser crashed during task execution",
    "type": "internal_error",
    "code": "browser_error"
  }
}
```

**Causes:**
- Browser crash
- Unexpected page behavior
- Network issues

**Solution:** Retry the request

---

### 503 Service Unavailable
```json
{
  "error": {
    "message": "No available workers",
    "type": "service_unavailable_error",
    "code": "no_workers"
  }
}
```

**Cause:** No workers connected to VPS

**Solution:** Start at least one worker

---

### 504 Gateway Timeout
```json
{
  "error": {
    "message": "No available worker within timeout",
    "type": "timeout_error",
    "code": "worker_timeout"
  }
}
```

**Cause:** All workers busy, 60-second timeout exceeded

**Solution:** 
- Add more workers
- Reduce request rate
- Retry after a moment

---

## 🔍 Monitoring

### Check System Status

```bash
# Quick health check
curl http://16.79.2.204:9000/health | jq '.status'

# Worker count
curl http://16.79.2.204:9000/health | jq '.workers.total_workers'

# Available accounts
curl http://16.79.2.204:9000/v1/models | jq '.data[].id'

# Busy slots
curl http://16.79.2.204:9000/health | jq '.workers.busy_slots'
```

### Monitor Response Times

Check `x_meta.response_time_ms` (worker scrape time) or `x_meta.response_time`
(end-to-end seconds) in responses:

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}' \
  | jq '.x_meta.response_time_ms'
```

---

## 🎓 Best Practices

### 1. Session Management
- Use unique `session_id` for each conversation thread
- Always use `mode: "new"` for the first message
- Use `mode: "continue"` for follow-up messages
- Session files are stored in `data/sessions/` on workers

### 2. Account Routing
- Use `preferred_account` to distribute load across specific accounts
- System automatically falls back if preferred account is busy
- Check `x_meta.account` to see which account was used

### 3. Error Handling
- Implement retry logic for 429, 500, 504 errors
- Exponential backoff recommended
- Log `x_meta.worker_id` for debugging

### 4. Performance
- Expected response times: 2-10 seconds depending on query complexity
- DeepThink mode takes longer (10-30 seconds)
- Vision mode with images takes 5-15 seconds

### 6. Mode & Tool Constraints
- **Instant mode**: Both tools available — DeepThink and Search
- **Expert mode**: Only DeepThink tool available — Search is hidden in UI
- **Vision mode**: Only DeepThink tool available — Search is hidden in UI
- Setting `"search": true` with `model_tab: "expert"` or `"vision"` is safe —
  the scraper silently ignores it (no error returned)

### 5. Rate Limits
- DeepSeek enforces per-account rate limits
- Use multiple accounts to increase throughput
- Monitor for 429 errors and back off

---

## 📊 Response Metadata Fields

All responses include an `x_meta` object with detailed information. Fields fall
into two groups: **VPS-level** (set by the gateway) and **worker `x_metadata`**
(folded in from the scraper, matching PAF-ModelQwen's `x_metadata` parity).

| Field | Group | Type | Description |
|-------|-------|------|-------------|
| `session_id` | VPS | string/null | Session identifier if persistence enabled |
| `mode` | VPS | string | Actual mode used by the worker: `"new"` or `"continue"` |
| `mode_fallback` | VPS | boolean | `true` if `continue` fell back to `new` (session missing/expired) |
| `account` | VPS | string | Which DeepSeek account was used |
| `conversation_url` | VPS | string | DeepSeek conversation URL (for debugging) |
| `model_tab` | VPS/worker | string | Tab used: `"instant"`, `"expert"`, or `"vision"` |
| `deep_think` | VPS/worker | boolean | Whether DeepThink was enabled |
| `web_search` | VPS/worker | boolean | Whether Search was enabled |
| `response_time` | VPS | float | End-to-end VPS processing time (seconds) |
| `timestamp` | VPS | float | Unix epoch when the response was built |
| `model` | worker | string | Account name (worker view) |
| `account_index` | worker | integer | Index of the account in the worker's rotation |
| `account_status` | worker | string | Account health at response time (e.g. `"ok"`) |
| `account_file` | worker | string | Profile dir backing the account |
| `retry_count` | worker | integer | Retries the worker performed for this request |
| `response_time_ms` | worker | integer | Worker scrape time in milliseconds |
| `think_mode` | worker | null | Always `null` for DeepSeek (uses Layer 1/Layer 2, not think_mode) |

> **Token usage** (`usage.prompt_tokens` / `completion_tokens` / `total_tokens`)
> is computed with `tiktoken` `cl100k_base` on the worker for accuracy, falling
> back to `len/4` only if `tiktoken` is unavailable — same approach as
> PAF-ModelQwen.

---

## 🔗 Integration Examples

### cURL + jq
```bash
RESPONSE=$(curl -s -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}')

echo $RESPONSE | jq -r '.choices[0].message.content'
echo $RESPONSE | jq -r '.x_meta.account'
```

### JavaScript/Node.js
```javascript
const axios = require('axios');

async function chat(message) {
  const response = await axios.post('http://16.79.2.204:9000/v1/chat/completions', {
    model: 'deepseek-chat',
    messages: [{role: 'user', content: message}]
  });
  
  return response.data.choices[0].message.content;
}

chat('Hello!').then(console.log);
```

### Python with Requests
```python
import requests

def chat(message, **kwargs):
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": message}],
        **kwargs
    }
    
    r = requests.post("http://16.79.2.204:9000/v1/chat/completions", json=payload)
    return r.json()["choices"][0]["message"]["content"]

# Simple usage
print(chat("What is AI?"))

# With options
print(chat("Explain quantum physics", think_mode="thinking"))
```

---

## 📚 Additional Resources

- **UPDATE_NOTES.md** - Complete feature documentation
- **DEPLOYMENT_GUIDE.md** - Setup and deployment instructions
- **CHANGELOG.md** - Version history and changes
- **HOTFIX_v2.0.1.md** - CLI event loop fix
- **HOTFIX_v2.0.2.md** - Browser close method fix

---

## 🆘 Support

For issues or questions:
1. Check `/health` endpoint for system status
2. Review worker logs for detailed error messages
3. Check `x_meta` fields in responses for debugging info
4. Consult the DEPLOYMENT_GUIDE.md for troubleshooting

---

**Version**: 2.5.0  
**Last Updated**: June 30, 2026  
**Base URL**: http://16.79.2.204:9000
