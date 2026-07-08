<picture>
  <source media="(prefers-color-scheme: dark)" srcset="web/assets/logo.png">
  <img alt="NVIDIA" src="web/assets/logo.png" width="200">
</picture>

# OpenVidia — NVIDIA Multi-Key Proxy

Minimal reverse proxy for the NVIDIA NIM API with web UI, automatic key rotation, model override, and live model testing.

## Quick Start

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
uv pip install -e .
openvidia              # daemon mode — starts proxy in background, opens browser
openvidia setup        # adds openvidia provider to opencode config
```

Opens `http://localhost:3940` automatically. The daemon runs silently in the background (no terminal output).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        openvidia daemon                         │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   Web UI (:3940)                        │    │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐                │    │
│  │  │ Home │ │ Keys │ │ News │ │ Settings │                │    │
│  │  └──────┘ └──────┘ └──────┘ └──────────┘                │    │
│  │  · Start/Stop proxy · Model presets · Activity log      │    │
│  │  · Key management · Real-time stats                     │    │
│  │  · Discourse News feed · Model browser + Test ▶        │    │
│  └─────────────────────────────────────────────────────────┘    │
│                           │                                     │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                   Proxy (:3940/v1)                      │    │
│  │  intercepts → overrides model → rotates keys → forwards │    │
│  │ default model stored in ~/.config/openvidia/active_model│    │
│  └─────────────────────────────────────────────────────────┘    │
│                           │                                     │
│                    NVIDIA NIM API                               │
│              integrate.api.nvidia.com/v1                        │
└─────────────────────────────────────────────────────────────────┘

opencode ──/"openvidia"──→ proxy ──/"deepseek-ai/..."──→ NVIDIA API
```

- **opencode** sees one model: `openvidia`. Select it with `/model openvidia`.
- **Web UI** controls which NVIDIA model the proxy overrides.
- **Proxy** intercepts every request, overrides the `model` field, rotates keys on 4xx/5xx.
- **No provider model list syncing.** No complex config.

## Commands

| Command | Description |
|---------|-------------|
| `openvidia` | Daemon mode — spawns background process, opens browser, exits silently |
| `openvidia foreground` | Foreground mode (used internally by daemon, useful for debugging) |
| `openvidia setup` | Adds `openvidia` provider to `~/.config/opencode/opencode.json` |

## Usage

### With opencode

1. `openvidia` — starts the proxy daemon
2. `opencode` → `/model openvidia`
3. Open `http://localhost:3940` → Home → Model Presets: click a model to activate
4. Settings → Available Models: browse, add to presets, or test any model

### With any OpenAI-compatible client

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:3940/v1", api_key="ignored")
r = client.chat.completions.create(
    model="openvidia",  # proxy overrides with the UI-selected model
    messages=[{"role":"user","content":"Hello!"}]
)
```

```bash
curl http://localhost:3940/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

## Web UI Tabs

| Tab | Features |
|-----|----------|
| **Home** | Status bar (proxy on/off, port, active model, request stats), **Model Presets** grid (click to switch), **Activity Log** with SSE live stream, **Start/Stop** buttons, **Restart** button |
| **Keys** | Add/remove API keys, per-key stats (requests, success/fail, freshness indicator, last error) |
| **News** | Fetches latest NVIDIA NIM forum posts + GLM-5.2 status thread from Discourse API (1h cache) |
| **Settings** | **Model Browser** with filter chips (★ Popular / All), search, **Test ▶** button per model, add to presets, usage examples |

### Proxy state control

- **Stop** — proxy returns `503 {"error":"proxy stopped"}`; web UI stays alive
- **Start** — resumes proxying
- **Restart** — spawns new process first, then kills old (no downtime window)

### Model browser filters

Default shows **★ Popular** — curated list of ~19 well-known models (DeepSeek, Meta, Mistral, Google, etc.). Switch to **All** to browse all 120+ NVIDIA NIM models.

### Test model

Click **▶** next to any model to test it directly against the NVIDIA API (bypasses the active model override). Uses a dedicated `/api/test-model` endpoint. Working models show green, DEGRADED/errors show red with the exact API error.

### NVIDIA logo

Official NVIDIA logo in the web UI header (replaces placeholder icon). Also displayed at the top of this README.

## Features

- **Multi-key rotation** — 9 keys, rotates on 4xx/5xx automatically, saves rotation index to disk
- **Model override** — set one model from the UI, every request goes through it
- **Persistent active model** — saved to `~/.config/openvidia/active_model`, restored on startup
- **Model presets** — save favorite models, switch instantly from Home
- **Real-time stats** — request count, rotations, success rate, SSE log stream
- **Per-key telemetry** — requests, success/fail, freshness (fresh/stale/unused), last error
- **Daemon mode** — runs silently in background, auto-opens browser
- **Start/Stop/Restart** — control proxy state without killing the web UI
- **News feed** — Discourse API scraper for NVIDIA NIM updates (1h cache)
- **NVIDIA brand colors** — official `#76B900` green, dark/light theme toggle
- **Health check** — `GET /health`
- **Lightweight** — FastAPI + httpx, single process, no external dependencies

## Keys

Add keys via the web UI (Keys tab) or edit `~/.config/openvidia/keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

If you have `accounts.json` from a previous version, keys are auto-extracted on first start.

## systemd (optional)

```bash
cp dist/openvidia.service ~/.config/systemd/user/
systemctl --user enable --now openvidia
```

## Config files

| File | Purpose |
|------|---------|
| `~/.config/openvidia/keys.json` | API keys (JSON array) |
| `~/.config/openvidia/presets.json` | Saved model presets |
| `~/.config/openvidia/active_model` | Currently active model (persists across restarts) |
| `~/.config/openvidia/index` | Key rotation index |
| `~/.config/openvidia/accounts.json` | Legacy accounts (auto-extracted to keys.json) |
| `~/.config/openvidia/news_cache.json` | News feed cache (1h TTL) |

Built with [FastAPI](https://fastapi.tiangolo.com/), [httpx](https://www.python-httpx.org/), [uvicorn](https://www.uvicorn.org/).
