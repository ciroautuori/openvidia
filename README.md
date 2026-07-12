# OpenVidia

**Multi-key proxy for NVIDIA NIM with a native desktop dashboard.**

Pool multiple free-tier API keys behind one endpoint. Automatic rotation, per-key cooldown, sliding-window RPM limiting, and a compact desktop app — no browser needed.

Built for [opencode](https://opencode.ai), [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Grok](https://x.ai), and any OpenAI-compatible client.

---

## Quick Start

### Linux (Ubuntu / Arch / Fedora)

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
./install.sh
```

Or manually:

```bash
# Install uv (recommended) — https://astral.sh/uv
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync                          # install dependencies
uv run openvidia setup           # auto-configure opencode
uv run openvidia                 # start proxy + desktop app
```

With pip:

```bash
pip install -e .
openvidia setup
openvidia
```

### macOS

```bash
brew install python@3.12 pygobject pkg-config
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
pip install -e .
openvidia setup
openvidia
```

> pywebview on macOS uses system WebKit (native, no extra deps).
> If you hit a GTK build error, install `pygobject` via Homebrew or just skip it — macOS doesn't need it.

### Windows

```cmd
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
pip install -e .
openvidia setup
openvidia
```

> pywebview on Windows uses EdgeChromium (WebView2, pre-installed on Windows 10/11).
> If WebView2 is missing, install it from [Microsoft](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

### Optional: Auto key regeneration

If you want keys to auto-regenerate when they die (requires a headless browser):

```bash
pip install -e ".[auto-regen]"
playwright install chromium
```

Without this, dead keys stay parked until you manually replace them.

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                     OpenVidia (:1919)                       │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Desktop App (pywebview)               │    │
│  │  310×570 native window — Keys, Presets, Models,     │    │
│  │  Activity log, CLI setup — all in one panel         │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│  ┌─────────────────────────────────────────────────────┐    │
│  │               Proxy Engine (:1919/v1)               │    │
│  │                                                     │    │
│  │  Request → override model → pick key → forward      │    │
│  │            ↑                ↑           ↑           │    │
│  │            │            cooldown?   RPM < 28?       │    │
│  │            │            skip if yes  skip if no     │    │
│  │            │                                        │    │
│  │  On 429: read Retry-After → set cooldown → next key │    │
│  │  On 401/403: cooldown 3600s (dead key)              │    │
│  │  On 5xx: cooldown 30s (transient)                   │    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                  │
│                   NVIDIA NIM API                            │
│            integrate.api.nvidia.com/v1                      │
└─────────────────────────────────────────────────────────────┘
```

---

## Why?

NVIDIA's free NIM tier limits each API key to ~40 RPM. Aggressive bursts trigger a **penalty box** that can lock keys for hours. OpenVidia:

- **Pools multiple keys** behind a single endpoint
- **Rotates automatically** on 429/401/403/5xx — zero manual intervention
- **Per-key cooldown timers** — respects `Retry-After` headers, exponential backoff
- **Sliding-window RPM limiting** — keeps each key under 28 RPM (safe margin below 40)
- **Health checks** — revives keys whose cooldowns have expired
- **Degraded fallback** — if a model fails on all keys, tries the next preset

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `openvidia` | Start proxy in background + open desktop app |
| `openvidia foreground` | Foreground mode (logs to stdout, no UI) |
| `openvidia setup` | Auto-configure opencode (provider, model, compaction, instructions) |

---

## CLI Setup Guides

The desktop app has a built-in **CLI Setup** tab with copy-paste instructions for:

| CLI | Protocol | Endpoint |
|-----|----------|----------|
| **opencode** | OpenAI-compatible | `http://localhost:1919/v1` |
| **Codex CLI** | OpenAI Responses API | `http://localhost:1919/v1/responses` |
| **Claude Code** | Anthropic Messages API | `http://localhost:1919/v1/messages` |
| **Grok (xAI)** | OpenAI-compatible | `http://localhost:1919/v1/chat/completions` |

### opencode

```bash
openvidia setup    # auto-configures provider + model + compaction
opencode           # launch with /model openvidia
```

### Codex CLI

Point Codex at the Responses API shim:

```bash
export OPENAI_BASE_URL=http://localhost:1919/v1
export OPENAI_API_KEY=ignored
codex --model openvidia
```

### Claude Code

Point Claude Code at the Anthropic Messages shim:

```bash
export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored
claude --model openvidia
```

> The `/v1/messages` endpoint translates Anthropic format ↔ OpenAI chat/completions bidirectionally (streaming, tool use, system prompts). Claude Code works unmodified.

### Grok (xAI)

Grok supports OpenAI-compatible providers natively:

```toml
# ~/.grok/config.toml
[provider.openvidia]
base_url = "http://localhost:1919/v1"
api_key = "ignored"
```

### Any OpenAI-compatible client

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:1919/v1", api_key="ignored")
response = client.chat.completions.create(
    model="openvidia",  # proxy overrides with the dashboard-selected model
    messages=[{"role": "user", "content": "Hello!"}]
)
```

```bash
curl http://localhost:1919/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

Streaming (SSE) is fully supported — tokens flow through unbuffered.

---

## Smart Rate Limiting

### Per-Key Cooldown

| HTTP Status | Cooldown | Reason |
|-------------|----------|--------|
| **429** | `Retry-After` header (or 60s) | Rate limited — respect NVIDIA's backoff |
| **401 / 403** | 3600s | Dead key — don't waste requests |
| **400 / 404** | 120s | Model access issue — might be temporary |
| **5xx** | 30s | Server error — retry soon |
| **Network error** | 30s | Transient connectivity issue |

### Sliding-Window RPM

Each key tracks requests in a rolling 60-second window. If a key has sent **28+ requests** in the last 60s, it's skipped. Only if all keys are saturated does the proxy return 429 to the client.

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
    └─ All keys exhausted? → try next preset model (degraded fallback)
```

### Health Check

Every 30 seconds:
1. Finds keys still on cooldown
2. Sends a lightweight `GET /v1/models` probe
3. If the key responds OK — clears the cooldown (revived)
4. If still failing — leaves the cooldown in place

---

## Desktop App

Native window via [pywebview](https://pywebview.flowrl.com/). Opens at **310×570 px** — a compact utility panel, like a phone in portrait. Resize freely.

| Backend | Platform | Engine |
|---------|----------|--------|
| **Qt WebEngine** | Linux (KDE/Wayland) | PyQt6-WebEngine (native, best experience) |
| **GTK WebKit** | Linux (GNOME/X11) | PyGObject + WebKitGTK |
| **WebKit** | macOS | system WebKit (no extra deps) |
| **EdgeChromium** | Windows | WebView2 (pre-installed on Win 10/11) |

pywebview auto-detects the best available backend.

### Linux desktop integration

```bash
# .desktop file (auto-installed by install.sh)
cp openvidia.desktop ~/.local/share/applications/
# Icon
cp web/assets/logo.png ~/.local/share/icons/hicolor/256x256/apps/openvidia.png
update-desktop-database ~/.local/share/applications/
```

---

## Dashboard Sections

| Section | Features |
|---------|----------|
| **Status** | Proxy state, active model, start/stop/restart controls |
| **Stats** | Request count, success rate, rotations, cooldown counter |
| **Presets** | Quick-switch model presets grid |
| **Keys** | Per-key status (Active filter default), live cooldown countdown, RPM, success/fail, freshness dots, add/remove/copy |
| **Models** | Filter chips, search, test ▶ any model, add to presets |
| **Activity** | Real-time SSE log stream with color-coded levels |
| **CLI Setup** | Copy-paste config for opencode / Codex / Claude / Grok |

### Key Status Indicators

| Indicator | Meaning |
|-----------|---------|
| 🟢 Green | Key healthy, has successful requests |
| 🟡 Amber | Key has failures but not on cooldown |
| ⚪ Gray | Key idle (no requests yet) |
| 🔴 Red + ⏳ | Key on cooldown — shows countdown + reason |
| `active` badge | Currently selected key in rotation |

---

## Configuration

### Config directory

| Platform | Path |
|----------|------|
| **Linux** | `~/.config/openvidia/` |
| **macOS** | `~/Library/Application Support/openvidia/` |
| **Windows** | `%APPDATA%\openvidia\` |

### Config files

| File | Purpose |
|------|---------|
| `keys.json` | API keys (JSON array) |
| `presets.json` | Saved model presets |
| `active_model` | Currently active model (persists across restarts) |
| `index` | Key rotation index |
| `accounts.json` | Legacy accounts (auto-extracted to keys.json) |

Add keys via the dashboard (**Keys** section) or edit `keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

### Rate limit tuning

Constants in `openvidia/proxy_state.py`:

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
| `POST` | `/v1/responses` | OpenAI Responses API shim (Codex CLI) |
| `POST` | `/v1/messages` | Anthropic Messages API shim (Claude Code) |
| `GET` | `/v1/models` | List available models from upstream |
| `GET` | `/health` | Health check — key count, port, status |

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Proxy running state + cooldown count |
| `GET` | `/api/stats` | Requests, rotations, success, cooldowns, total RPM |
| `GET` | `/api/keys/stats` | Per-key: requests, success/fail, cooldown, RPM, reason |
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
| `GET/POST` | `/api/accounts` | Manage legacy accounts (auto-regen) |
| `POST` | `/api/accounts/active` | Set active account |

---

## Tech Stack

- **[FastAPI](https://fastapi.tiangola.com/)** — async web framework
- **[httpx](https://www.python-httpx.org/)** — HTTP/2 client for upstream
- **[uvicorn](https://www.uvicorn.org/)** — ASGI server
- **[pywebview](https://pywebview.flowrl.com/)** — native desktop window (Qt/GTK/WebKit/EdgeChromium)
- **[psutil](https://github.com/giampaolo/psutil)** — cross-platform process management
- **Vanilla HTML/CSS/JS** — zero frontend build, no node_modules
- **Python 3.12+** — single process, no external services

---

## License

MIT

---

Built by [Ciro Autuori](https://github.com/ciroautuori).
