# OpenVidia — OpenWiki Quickstart

**OpenVidia** is a multi-key reverse proxy for the NVIDIA NIM API with a native desktop dashboard. It pools multiple free-tier API keys behind a single localhost endpoint, handling automatic rotation, per-key cooldowns, sliding-window RPM limiting, and auto-compaction — all behind a compact desktop app (no browser needed).

Built for [opencode](https://opencode.ai), [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Grok](https://x.ai), and any OpenAI-compatible client.

---

## What it does

NVIDIA's free NIM tier limits each API key to ~40 RPM. Aggressive bursts can lock keys for hours. OpenVidia:

- **Pools multiple keys** behind `http://localhost:1919/v1`
- **Rotates automatically** on 429 / 401 / 403 / 5xx — zero manual intervention
- **Respects `Retry-After`** headers with per-key cooldown timers
- **Keeps each key under 28 RPM** via a sliding 60-second window
- **Revives keys** via background health checks (every 30s)
- **Never blocks on context overflow** — auto-compaction summarizes long histories before forwarding
- **Falls back to the next starred preset model** if the active model fails on all keys
- **Translates API formats** for Codex (Responses API) and Claude Code (Anthropic Messages) bidirectionally

---

## Quick start

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
uv sync                     # or: pip install -e .
uv run openvidia setup      # auto-configure opencode, Codex, Grok
uv run openvidia             # start proxy + desktop app
```

Add keys via the dashboard **Keys** section, or edit `~/.config/openvidia/keys.json`:

```json
["nvapi-xxx", "nvapi-yyy"]
```

Then point any client at `http://localhost:1919/v1`:

```bash
curl http://localhost:1919/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ignored" \
  -d '{"model":"openvidia","messages":[{"role":"user","content":"Hello!"}]}'
```

The proxy overrides `model: "openvidia"` with the dashboard-selected model. Any other model string passes through directly.

---

## CLI commands

| Command | Description |
|---------|-------------|
| `openvidia` | Start proxy in background + open desktop app |
| `openvidia foreground` | Foreground mode (logs to stdout, no UI) |
| `openvidia setup` | Auto-configure detected CLIs (opencode, Codex, Grok) |

---

## Supported clients

| Client | Protocol | Endpoint |
|--------|----------|----------|
| **opencode** | OpenAI-compatible | `http://localhost:1919/v1` |
| **Codex CLI** | OpenAI Responses API | `http://localhost:1919/v1/responses` |
| **Claude Code** | Anthropic Messages API | `http://localhost:1919/v1/messages` |
| **Grok (xAI)** | OpenAI-compatible | `http://localhost:1919/v1/chat/completions` |

See `openvidia setup` for automatic configuration, or [Operations](operations.md) for manual setup details.

---

## Architecture in brief

```
Client request → FastAPI (:1919)
  ├─ /v1/responses  → Responses shim → chat/completions (Codex)
  ├─ /v1/messages   → Anthropic shim  → chat/completions (Claude Code)
  └─ /v1/{path}     → catch-all proxy
      ├─ model override ("openvidia" → active model)
      ├─ auto-compaction (if history exceeds budget)
      ├─ key rotation (skip cooldown / RPM-saturated keys)
      ├─ forward to NVIDIA NIM (streaming passthrough)
      └─ on failure: mark_key_failed → rotate to next key
```

For the full breakdown — proxy engine, shims, key rotation, compaction, desktop app — see [Architecture](architecture.md).

---

## Key source map

| File | Responsibility |
|------|----------------|
| `openvidia/__main__.py` | CLI entrypoint (`main`), proxy lifecycle, desktop window (pywebview), system tray, CLI auto-setup (opencode/Codex/Grok) |
| `openvidia/proxy_app.py` | FastAPI app factory, catch-all proxy handler, key rotation loop, model override, health check, streaming passthrough |
| `openvidia/proxy_state.py` | `ProxyState` — keys, cooldowns, RPM trackers, `get_candidate_keys()`, stats, SSE log push, thread-safe key mutations |
| `openvidia/responses_shim.py` | OpenAI Responses API → chat/completions translation (for Codex CLI) |
| `openvidia/anthropic_shim.py` | Anthropic Messages API → chat/completions translation (for Claude Code) |
| `openvidia/compaction.py` | Auto-compaction: summarize long histories via upstream call + roll-forward cache, trim fallback |
| `openvidia/webui.py` | Dashboard static-file serving + all `/api/*` endpoints (keys, model, presets, stats, logs, stop/start/restart) |
| `openvidia/config.py` | Cross-platform config paths, atomic file writes, key/preset/index/stop-flag persistence |
| `openvidia/server_manager.py` | Uvicorn server startup, dual-stack (IPv4+IPv6) loopback binding |
| `openvidia/account_manager.py` | Auto key regeneration via CDP or Playwright when keys die (401/403) |
| `openvidia/cdp_keygen.py` | Chrome DevTools Protocol key generation (primary auto-regen path) |
| `openvidia/key_factory.py` | Playwright-based key generation (fallback auto-regen path) |
| `web/` | Vanilla HTML/CSS/JS dashboard (no build step) |

---

## Where to go next

- **[Architecture](architecture.md)** — proxy engine, key rotation, API shims, compaction, desktop app internals
- **[Operations](operations.md)** — installation, config files, CLI setup, API reference, rate-limit tuning, desktop backends
- The repository [README.md](../README.md) has detailed rate-limiting tables, client setup snippets, and dashboard screenshots
