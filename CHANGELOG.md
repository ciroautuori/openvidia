# Changelog

All notable changes to OpenVidia will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `model_budgets` guidance in the README: NVIDIA NIM does not advertise a
  context window, but an oversized request answers with the exact number
- `inline_deadline` — an upper bound on how long a client waits for
  compaction, independent of upstream latency
- `summary_model` — summarization runs on a separate, fast model so it never
  competes for keys with the stream the agent is saturating
- `compact_ratio` — compact below the trigger instead of onto it
- Regression tests for the rolling cache, the latency budget, and concurrent
  compaction of the same conversation (81 tests total)
- Declared dev dependencies, so a fresh clone runs `uv run pytest` directly
- Unit tests for proxy rotation, cooldown management, and compaction
- GitHub issue templates for bugs, features, enhancements, and questions
- CONTRIBUTING.md guide for new contributors
- SECURITY.md with vulnerability disclosure process
- Error logging improvements for better debugging

### Changed
- Compaction serves a cached summary plus every later message verbatim while
  it fits the budget, so the steady state costs zero upstream calls
- The verbatim tail is sized to fill the budget; `keep_recent` is now only a
  floor for the trim fallback
- A summarize slower than the deadline continues detached and lands in the
  cache for the next turn instead of blocking the request
- Concurrent requests on one conversation share a single summarize
- Version bumped from 2.0.0 to 1.0.0 (first stable release)
- Test suite uses pytest with async support

### Fixed
- **Compaction re-summarized the whole history every turn.** The rolling cache
  could never hit: the conversation key included the message count (new key
  each turn) and the stored fingerprint was compared against a longer prefix
  than it covered. Summaries blew the timeout and every request silently fell
  back to trimming
- **Restarts failed silently.** `SIGTERM` alone does not stop uvicorn while a
  client holds an SSE stream open; the launcher waited 3s and started anyway,
  leaving the previous build answering every request on the port. It now
  escalates to `SIGKILL`, verifies the port is free, and refuses to start
  otherwise
- The desktop launcher waits for the proxy to answer instead of `sleep(3)`,
  and reports the exit code when the server dies during startup
- Tray "Quit" now actually stops a proxy with active streams
- `_trim()` is O(n) instead of O(n²) on its safety loops
- `.gitignore` was wrapped in Markdown fences; build artifacts (`dist/`,
  `*.egg-info/`) are no longer tracked
- Cooldown key handling in candidate selection
- Token estimation edge cases in compaction

---

## [1.0.0] - 2025-01-XX

### Added
- **Multi-key proxy** with intelligent rotation across NVIDIA NIM API keys
- **Adaptive rate limiting** with per-key RPM tracking (28 RPM safe limit)
- **Automatic cooldown management** based on HTTP status codes:
  - 401/403: 1 hour (invalid keys)
  - 429: 3 minutes with jittered backoff (rate-limited)
  - 400/404: 2 minutes (bad requests)
  - 5xx: 30 seconds (server errors)
- **Auto-compaction** for conversation history to prevent context overflow
- **Health check system** with background probing of cooldown-expired keys
- **Weighted load balancing** - prefers least-loaded keys (in-flight + RPM)
- **Desktop dashboard** with real-time stats and key management
- **Web UI** accessible at `http://localhost:3940`
- **OpenAI-compatible API** shim for seamless integration with:
  - VS Code Copilot / Codex
  - Claude Code (via Anthropic Messages shim)
  - Any OpenAI SDK client
- **Cross-platform installation** script for Linux, macOS, and Windows
- **PyPI package** - installable via `pip install openvidia`
- **Configuration management** with JSON-based settings
- **SSE logging** for real-time dashboard updates
- **Key persistence** with atomic writes

### Changed
- httpx timeout configuration for bounded rotation attempts (max 5 attempts)
- Pool saturation detection (<20% live keys skips rotation)
- Adaptive RPM ceiling halving on 429 responses
- Graceful RPM rehabilitation (+4 RPM per successful window)

### Technical Details
- **Python 3.12+** required
- **FastAPI** for async HTTP server
- **httpx with HTTP/2** for connection reuse
- **Threading + asyncio locks** for thread-safe state management
- **Sliding window** RPM tracking (60-second window)
- **SHA-256 fingerprints** for conversation cache identity

### Architecture
```
openvidia/
├── proxy_app.py      # Main proxy logic, catch-all route, streaming
├── proxy_state.py    # Thread-safe shared state, cooldowns, RPM tracking
├── compaction.py     # Auto-summarization for long conversations
├── config.py         # Configuration management
├── key_factory.py    # Key validation and management
├── account_manager.py # Account/key provisioning helpers
├── server_manager.py # Server lifecycle management
├── webui.py          # Dashboard Web UI
├── responses_shim.py # OpenAI Responses API → chat/completions
└── anthropic_shim.py # Anthropic Messages API compatibility
```

---

## [0.x.x] - Pre-release

Initial development versions with core proxy functionality.
