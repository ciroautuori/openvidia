# OpenWiki Plan — openvidia

## Repository summary
OpenVidia is a multi-key reverse proxy for NVIDIA NIM API. Pools multiple free-tier API keys behind a single localhost endpoint (:1919/v1), with automatic key rotation, per-key cooldown, sliding-window RPM limiting, auto-compaction, API shims (OpenAI Responses for Codex, Anthropic Messages for Claude Code), and a native desktop dashboard (pywebview). Built for opencode, Codex CLI, Claude Code, Grok, and any OpenAI-compatible client.

## Wiki pages to create

### 1. quickstart.md (entrypoint)
- High-level overview: what, why, how
- Install (3 platforms)
- Quick usage examples
- Links to all other pages
**Source evidence:** README.md, pyproject.toml, install.sh, openvidia/__main__.py

### 2. architecture.md
- Proxy engine (proxy_app.py): request flow, key rotation, streaming
- Shared state (proxy_state.py): KeyState, KeyCooldown, RpmTracker, ProxyState, get_candidate_keys
- API shims: anthropic_shim.py, responses_shim.py — translation layers, sanitization
- Auto-compaction (compaction.py): summarize+trim strategy, roll-forward cache
- Desktop app & webui (webui.py, __main__.py): pywebview backends, tray, server manager
- Account manager & key generation (account_manager.py, cdp_keygen.py, key_factory.py)
**Source evidence:** all openvidia/*.py files

### 3. operations.md
- Install & setup (install.sh, CLI setup for opencode/Codex/Claude/Grok)
- Configuration files (keys.json, presets.json, active_model, index, compaction.json)
- Config directory paths per platform (config.py)
- API endpoints reference (proxy + dashboard)
- Rate limit tuning (constants in proxy_state.py)
- Desktop app backends
**Source evidence:** config.py, __main__.py, webui.py, proxy_app.py, README.md

## Key details to capture
- Port: 1919 (loopback only, IPv4 + IPv6)
- Upstream: https://integrate.api.nvidia.com/v1/
- MAX_RPM = 28, RPM_WINDOW = 60s
- COOLDOWN_DURATIONS: 400→120s, 401/403→3600s, 404→120s, 429→180s, default→30s
- ROTATE_STATUSES = {401, 403, 429} + >=500
- Model override: "openvidia" → active_model or DEFAULT_MODEL (deepseek-ai/deepseek-v4-pro)
- Compaction: budget 100k tokens, keep_recent 8, all client endpoints covered
- threading.Lock for cross-thread key mutations (account_manager → event loop)

## Questions / open items
- None blocking; README is comprehensive, will defer to it for detail and link.
