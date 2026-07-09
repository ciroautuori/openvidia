<picture>
  <source media="(prefers-color-scheme: dark)" srcset="web/assets/logo.png">
  <img alt="OpenVidia" src="web/assets/logo.png" width="200">
</picture>

# OpenVidia — NVIDIA NIM Multi-Key Proxy with Smart Rate Limiting

A lightweight reverse proxy for the [NVIDIA NIM API](https://build.nvidia.com) with a built-in web dashboard, intelligent per-key cooldown management, sliding-window RPM tracking, automatic key rotation, model override, and live model testing.

Designed for [opencode](https://opencode.ai), works with **any OpenAI-compatible client**.

---

## Why?

NVIDIA's free NIM tier limits each API key to ~40 RPM. Aggressive bursts trigger a **penalty box** that can lock keys for hours. OpenVidia solves this by:

- **Pooling multiple keys** behind a single endpoint
- **Rotating automatically** on 429/401/403/5xx — no manual intervention
- **Per-key cooldown timers** — respects `Retry-After` headers, uses exponential backoff
- **Sliding-window RPM limiting** — keeps each key under 28 RPM (safe margin below 40)
- **Health checks** — periodically revives keys whose cooldowns have expired

All manageable from a real-time web dashboard at `http://localhost:1919`.

---

## Quick Start

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
./install.sh
```

That's it. `install.sh` does everything:

1. Installs Python dependencies (`uv sync` or `pip install -e .`)
2. Installs the `openvidia` launcher to `~/.local/bin/`
3. Auto-configures opencode (provider, model, compaction, instructions)
4. Starts the proxy and verifies it's running

Or install manually:

```bash
uv pip install -e .
openvidia setup        # configure opencode provider + compaction
openvidia              # start daemon, opens browser automatically
```

> Add keys via the dashboard (**Keys** tab) or edit `~/.config/openvidia/keys.json`:
> ```json
> ["nvapi-xxx", "nvapi-yyy", "..."]
> ```

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                     OpenVidia (:1919)                       │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  Web Dashboard                       │    │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐           │    │
│  │  │ Home │ │ Keys │ │ News │ │ Settings │           │    │
│  │  └──────┘ └──────┘ └──────┘ └──────────┘           │    │
│  │  · Model presets   · Per-key cooldown + RPM       │    │
│  │  · Activity log    · Start/Stop/Restart            │    │
│  │  · Model browser   · Test ▶ any model             │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Proxy Engine (:1919/v1)               │    │
│  │                                                     │    │
│  │  Request → override model → pick key → forward     │    │
│  │            ↑                ↑           ↑            │    │
│  │            │            cooldown?   RPM < 28?       │    │
│  │            │            skip if yes  skip if no     │    │
│  │            │                                         │    │
│  │  On 429: read Retry-After → set cooldown → next key │    │
│  │  On 401/403: cooldown 3600s (dead key)              │    │
│  │  On 5xx: cooldown 30s (transient)                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                   NVIDIA NIM API                            │
│            integrate.api.nvidia.com/v1                      │
└─────────────────────────────────────────────────────────────┘

opencode ──"openvidia"──→ proxy ──"deepseek-ai/..."──→ NVIDIA
```

---

## Smart Rate Limiting

OpenVidia implements a multi-layered rate-limit strategy to maximize throughput while avoiding NVIDIA's penalty box:

### Per-Key Cooldown

| HTTP Status | Cooldown | Reason |
|-------------|----------|--------|
| **429** | `Retry-After` header (or 60s default) | Rate limited — respect NVIDIA's backoff |
| **401 / 403** | 3600s | Dead key — don't waste requests |
| **400 / 404** | 120s | Model access issue — might be temporary |
| **5xx** | 30s | Server error — retry soon |
| **Network error** | 30s | Transient connectivity issue |

### Sliding-Window RPM

Each key tracks requests in a rolling 60-second window. If a key has sent **28+ requests** in the last 60s, it's skipped in favor of another key. Only if all keys are saturated does the proxy return 429 to the client.

### Key Rotation Flow

```
Request arrives
    │
    ├─ Key on cooldown? → skip, try next
    ├─ Key RPM ≥ 28?   → skip, try next
    ├─ Send to NVIDIA  → 200? ✅ record RPM, return response
    │                  → 429? read Retry-After, set cooldown, rotate
    │                  → 401? set 3600s cooldown, rotate
    │                  → 5xx? set 30s cooldown, rotate
    │
    └─ All keys exhausted? → degraded model fallback (if configured)
```

### Health Check

Every 30 seconds, the background health check:
1. Identifies keys still on cooldown
2. Sends a lightweight `GET /v1/models` probe
3. If the key responds OK — clears the cooldown (revived)
4. If still failing — leaves the existing cooldown in place (no overwrite)

This ensures recovering keys return to rotation quickly while dead keys stay parked.

---

## Usage

### With opencode

```bash
openvidia            # start the proxy daemon
opencode             # launch opencode
```

`openvidia setup` auto-configures:
- **Provider**: `openvidia` → `http://localhost:1919/v1`
- **Model**: `openvidia/openvidia` (default + small model)
- **Compaction**: `auto: true, prune: true, reserved: 8000`
- **Instructions**: `AGENTS.md` (if present)

In opencode, select `/model openvidia`. Then use the web dashboard to switch NVIDIA models in real time — no opencode restart needed.

### With any OpenAI-compatible client

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:1919/v1", api_key="ignored")
response = client.chat.completions.create(
    model="openvidia",  # proxy overrides with the dashboard-selected model
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

```bash
curl http://localhost:1919/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

Streaming (SSE) is fully supported — tokens flow through the proxy unbuffered.

---

## Web Dashboard

| Tab | Features |
|-----|----------|
| **Home** | Proxy status, request/rotation/success stats, **cooldown counter** (amber + blinking when keys are on cooldown), model presets grid, live SSE activity log, start/stop/restart controls |
| **Keys** | Per-key status with **live cooldown countdown** (⏳ Ns + reason), **RPM per key**, success/fail counts, freshness indicators (green/amber/red dots), add/remove/copy keys |
| **News** | NVIDIA NIM forum feed + GLM-5.2 status thread (1h cache, Discourse API) |
| **Settings** | Model browser with filter chips (★ Popular / All), search, **Test ▶** button per model, add to presets, usage examples (cURL / Python / JS) |

### Key Status Indicators

| Indicator | Meaning |
|-----------|---------|
| 🟢 **Green dot** | Key healthy, has successful requests |
| 🟡 **Amber dot** | Key has failed requests but not on cooldown |
| ⚪ **Gray dot** | Key idle (no requests yet) |
| 🔴 **Red dot + ⏳** | Key on cooldown — shows countdown + reason |
| `active` badge | Currently selected key in rotation |
| `28 RPM` | Current requests per minute for this key |

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `openvidia` | Daemon mode — spawns background process, opens browser, exits silently |
| `openvidia foreground` | Foreground mode (debugging — prints logs to terminal) |
| `openvidia setup` | Auto-configures opencode (provider, model, compaction, instructions) |

---

## Degraded Model Fallback

If a model fails on all keys, OpenVidia can automatically retry with a fallback model:

| Original Model | Fallback Model |
|----------------|----------------|
| `z-ai/glm-5.2` | `deepseek-ai/deepseek-v4-pro` |
| `moonshotai/kimi-k2.6` | `deepseek-ai/deepseek-v4-flash` |

This handles cases where NVIDIA removes or restricts access to a model on free-tier keys.

---

## Configuration

### Config files

| File | Purpose |
|------|---------|
| `~/.config/openvidia/keys.json` | API keys (JSON array) |
| `~/.config/openvidia/presets.json` | Saved model presets |
| `~/.config/openvidia/active_model` | Currently active model (persists across restarts) |
| `~/.config/openvidia/index` | Key rotation index |
| `~/.config/openvidia/accounts.json` | Legacy accounts (auto-extracted to keys.json) |
| `~/.config/openvidia/news_cache.json` | News feed cache (1h TTL) |

### Rate limit tuning

All constants are in `openvidia/proxy_state.py`:

```python
MAX_RPM = 28              # Safe margin below NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0         # Sliding window in seconds

COOLDOWN_DURATIONS = {
    400: 120.0,           # Bad request — model access
    401: 3600.0,          # Unauthorized — dead key
    403: 3600.0,          # Forbidden — dead key
    404: 120.0,           # Not found — model not on this key
    429: 60.0,            # Rate limited (Retry-After overrides)
}
DEFAULT_COOLDOWN = 30.0   # Network errors, unknown 5xx
```

---

## API Endpoints

### Proxy

| Method | Path | Description |
|--------|------|-------------|
| `*` | `/v1/{path}` | Forward to NVIDIA NIM (streaming supported) |
| `GET` | `/health` | Health check — key count, port, status |

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Proxy running state + cooldown count |
| `GET` | `/api/stats` | Requests, rotations, success, cooldowns, total RPM |
| `GET` | `/api/keys/stats` | Per-key: requests, success/fail, **cooldown**, **RPM**, reason |
| `GET` | `/api/keys` | List keys |
| `POST` | `/api/keys` | Replace all keys |
| `POST` | `/api/keys/add` | Add a key |
| `POST` | `/api/keys/remove` | Remove a key |
| `GET/POST` | `/api/model` | Get/set active model override |
| `GET/POST` | `/api/presets` | Get/save model presets |
| `POST` | `/api/test-model` | Test a model directly (bypasses override) |
| `POST` | `/api/stop` | Stop proxy (returns 503 to clients) |
| `POST` | `/api/start` | Resume proxy |
| `POST` | `/api/restart` | Zero-downtime restart (spawn new, kill old) |
| `GET` | `/api/logs/stream` | SSE log stream (real-time) |
| `GET` | `/api/news` | NVIDIA forum news (1h cache) |

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangola.com/)** — async web framework
- **[httpx](https://www.python-httpx.org/)** — HTTP/2 client for upstream requests
- **[uvicorn](https://www.uvicorn.org/)** — ASGI server
- **Vanilla HTML/CSS/JS** — zero frontend build, no node_modules
- **Python 3.12+** — single process, no external services

---

## License

MIT

---

## Contributing

PRs welcome. Key areas:
- Additional fallback model mappings
- Prometheus/Grafana metrics export
- Support for non-NVIDIA upstreams
- Tauri desktop wrapper (WIP in `web/src-tauri/`)

Built with care by [Ciro Autuori](https://github.com/ciroautuori).
