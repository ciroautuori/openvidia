# OpenVidia — NVIDIA Multi-Key Proxy

Minimal reverse proxy for NVIDIA NIM API with web UI and automatic key rotation.

## Quick Start

```bash
cd ~/Scrivania/envidia

# Add keys manually
echo '["nvapi-xxx","nvapi-yyy"]' > ~/.config/openvidia/keys.json

# Run
uv run python3 -m openvidia
```

Open http://localhost:3940 — add/remove keys from the web UI.
