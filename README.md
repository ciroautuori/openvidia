# OpenVidia — NVIDIA Multi-Key Proxy

Minimal reverse proxy for NVIDIA NIM API with web UI, automatic key rotation, and model browser.

## Quick Start

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
uv pip install -e .
openvidia
```

Opens `http://localhost:3940` automatically.

## Usage

Point any OpenAI-compatible client to `http://localhost:3940/v1`:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:3940/v1", api_key="ignored")
r = client.chat.completions.create(
    model="minimaxai/minimax-m3",
    messages=[{"role":"user","content":"Hello!"}]
)
print(r.choices[0].message.content)
```

```bash
curl http://localhost:3940/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"minimaxai/minimax-m3","messages":[{"role":"user","content":"Hello!"}]}'
```

## Features

- **Multi-key rotation** — 9 keys, rotates on 4xx/5xx automatically
- **Web UI** — manage keys, model override, real-time stats + log stream
- **Model presets** — Passthrough, GLM 5.2, DeepSeek V4 Pro, MiniMax M3
- **Model browser** — browse all available NVIDIA NIM models, click to activate
- **Per-key stats** — requests, success/fail, freshness indicator, last error
- **Dark/light theme** — toggle in Settings
- **Health check** — `GET /health`

## Keys

Add keys via the web UI (Keys tab) or edit `~/.config/openvidia/keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

If you have `accounts.json` from a previous version, keys are auto-extracted.

## systemd (optional)

```bash
cp dist/openvidia.service ~/.config/systemd/user/
systemctl --user enable --now openvidia
```

## Project

**`main`** branch — Python (FastAPI), the only maintained version.  
**`rust`** branch — legacy Rust version, no longer maintained.
