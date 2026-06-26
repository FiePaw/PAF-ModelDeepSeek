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

List all available DeepSeek accounts across all workers.

**Request:**
```bash
curl http://16.79.2.204:9000/v1/models
```

**Response:**
```json
{
  "object": "list",
  "data": [
    {
      "id": "account1",
      "object": "model",
      "created": 1719374917,
      "owned_by": "deepseek"
    },
    {
      "id": "account2",
      "object": "model",
      "created": 1719374917,
      "owned_by": "deepseek"
    },
    {
      "id": "account3",
      "object": "model",
      "created": 1719374917,
      "owned_by": "deepseek"
    }
  ]
}
```

**Notes:**
- Each account represents a logged-in DeepSeek browser
- The `id` field is the account name that can be used with `preferred_account`

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
      "role": "user",                 // Required: "user", "assistant", or "system"
      "content": "Your message here"  // Required: Message content
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

#### think_mode Aliases

Simplify mode selection with these aliases:

| think_mode | → model_tab | → deep_think | → search |
|------------|-------------|--------------|----------|
| `"instant"` | `"instant"` | `false` | `false` |
| `"thinking"` | `"expert"` | `true` | `false` |
| `"deep"` | `"expert"` | `true` | `false` |
| `"search"` | `"expert"` | `false` | `true` |
| `"vision"` | `"vision"` | `false` | `false` |

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

```json
{
  "id": "req-abc123def456",
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
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  },
  "x_meta": {
    "session_id": "my-session",
    "account_name": "account1",
    "conversation_url": "https://chat.deepseek.com/chat/abc123",
    "model_tab": "expert",
    "deep_think": true,
    "search": false,
    "worker_id": "worker-DESKTOP-ABC123-001",
    "processing_time_ms": 3245
  }
}
```

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
    "account_name": "account1",
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
- Response includes actual account used in `x_meta.account_name`

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

### Example 4: Deep Thinking Mode

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
- Uses Expert tab with DeepThink enabled
- DeepSeek will show its reasoning process
- Response includes full reasoning + answer

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

Check `x_meta.processing_time_ms` in responses:

```bash
curl -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}' \
  | jq '.x_meta.processing_time_ms'
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
- Check `x_meta.account_name` to see which account was used

### 3. Error Handling
- Implement retry logic for 429, 500, 504 errors
- Exponential backoff recommended
- Log `x_meta.worker_id` for debugging

### 4. Performance
- Expected response times: 2-10 seconds depending on query complexity
- DeepThink mode takes longer (10-30 seconds)
- Vision mode with images takes 5-15 seconds

### 5. Rate Limits
- DeepSeek enforces per-account rate limits
- Use multiple accounts to increase throughput
- Monitor for 429 errors and back off

---

## 📊 Response Metadata Fields

All responses include an `x_meta` object with detailed information:

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string/null | Session identifier if persistence enabled |
| `account_name` | string | Which DeepSeek account was used |
| `conversation_url` | string | DeepSeek conversation URL (for debugging) |
| `model_tab` | string | Tab used: "instant", "expert", or "vision" |
| `deep_think` | boolean | Whether DeepThink was enabled |
| `search` | boolean | Whether search was enabled |
| `worker_id` | string | Which worker processed the request |
| `processing_time_ms` | integer | Time taken to process (milliseconds) |

---

## 🔗 Integration Examples

### cURL + jq
```bash
RESPONSE=$(curl -s -X POST http://16.79.2.204:9000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}]}')

echo $RESPONSE | jq -r '.choices[0].message.content'
echo $RESPONSE | jq -r '.x_meta.account_name'
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

**Version**: 2.0.2  
**Last Updated**: June 26, 2026  
**Base URL**: http://16.79.2.204:9000
