# OpenVidia — NVIDIA Multi-Key Proxy

Minimal reverse proxy for the NVIDIA NIM API with web UI, automatic key rotation, model override, and live model testing.

## Quick Start

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
uv pip install -e .
openvidia                     # starts proxy + auto-opens browser
openvidia setup               # adds openvidia provider to opencode
```

Opens `http://localhost:3940` automatically.

## How It Works

```
opencode ──model:"openvidia"──→ proxy (:3940) ──model:"deepseek-ai/..."──→ NVIDIA API
```

- **opencode** sees one model: `openvidia`. You select it with `/model openvidia`.
- **UI** controls which actual NVIDIA model the proxy uses via model override.
- **Proxy** intercepts every request and overrides the model field before forwarding to NVIDIA.
- No provider model list syncing. No complex config.

## Usage

### With opencode

1. `openvidia` — start the proxy
2. In opencode: `/model openvidia`
3. Open `http://localhost:3940` in browser
4. Home → **Model Presets**: click a model to activate it
5. Settings → **Available Models**: browse, add to presets, or test any model

### With any OpenAI-compatible client

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:3940/v1", api_key="ignored")
r = client.chat.completions.create(
    model="openvidia",  # proxy overrides this with the UI-selected model
    messages=[{"role":"user","content":"Hello!"}]
)
print(r.choices[0].message.content)
```

```bash
curl http://localhost:3940/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

## Web UI

| Tab     | Content |
|---------|---------|
| Home    | Status bar + model presets (click to switch) + usage example + activity log |
| Keys    | View/add/remove API keys, per-key stats (success/fail/last error) |
| Settings | Available model browser with **filter chips** (★ Popular / All), **Test** button (▶) for each model, search, usage examples |

### Model browser filters

Default shows **★ Popular** — a curated list of ~19 well-known models (DeepSeek, Meta, Mistral, Google, etc.). Switch to **All** to browse all 120+ NVIDIA NIM models.

### Test model

Click **▶** next to any model in the browser to run a quick chat completion. Working models show green, DEGRADED/errors show red with the exact API error.

## Features

- **Multi-key rotation** — 9 keys, rotates on 4xx/5xx automatically, saves rotation index
- **Model override** — set one model from the UI, every request goes through it
- **Model presets** — save your favorite models, switch instantly from Home
- **Real-time stats** — request count, rotations, success rate, SSE log stream
- **Per-key telemetry** — requests, success/fail, freshness (fresh/stale/unused), last error
- **NVIDIA brand colors** — official `#76B900` green, dark/light theme toggle
- **Keyboard shortcuts** — `Enter` to confirm key add
- **Health check** — `GET /health`

## Keys

Add keys via the web UI (Keys tab) or edit `~/.config/openvidia/keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

If you have `accounts.json` from a previous version, keys are auto-extracted on first start.

### Known model status

| Model | Status |
|-------|--------|
| `deepseek-ai/deepseek-v4-flash` | ✅ Works |
| `deepseek-ai/deepseek-v4-pro` | ✅ Works (slower) |
| `z-ai/glm-5.2` | ❌ `DEGRADED` (NVIDIA server-side, see [forum](https://forums.developer.nvidia.com/t/model-glm-5-2-showing-error-400/375867)) |
| `minimaxai/minimax-m3` | ❌ `DEGRADED` (intermittent) |

## systemd (optional)

```bash
cp dist/openvidia.service ~/.config/systemd/user/
systemctl --user enable --now openvidia
```

## Config files

| File | Purpose |
|------|---------|
| `~/.config/openvidia/keys.json` | API keys |
| `~/.config/openvidia/presets.json` | Saved model presets |
| `~/.config/openvidia/index` | Key rotation index |
| `~/.config/openvidia/accounts.json` | Legacy accounts (auto-extract) |

## Project

**`main`** branch — Python (FastAPI), the only maintained version.  
**`rust`** and **`python`** branches — legacy, no longer maintained.
