# Changelog

All notable changes to OpenVidia will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-08-01

### Added
- **Thinking toggle** — `auto` / `on` / `off` next to the active model. A
  hybrid reasoning model emits nothing while it thinks, which is the
  difference between a 2s and a 160s first token. Per-model, stored
  server-side so every CLI picks it up. The flag's *name* lives in
  `model_options.json`, not in code: a future model that wants
  `reasoning_effort` instead needs a config edit, not a release
- **Context windows are learned, not configured** — the proxy asks the
  provider once, caches the answer in `model_limits.json`, and also harvests
  it from any real overflow error. A model added by the provider tomorrow
  runs at full context with no configuration
- **Per-model health from live traffic** (`/api/model-health`) — success
  rate, median time to first token, gateway timeouts and 429s, measured from
  requests that were happening anyway. When a model degrades the Activity log
  names the cause and says it is the provider struggling, not your keys
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
- **Graph Engine** — `openvidia.graph_engine`: a `Hub` with mailboxes, `run_agent`
  with `max_iterations` / `budget_tokens` / `timeout_s` caps, spawn-and-kill
  subagent tools with a depth bound, and `generate_and_verify` generator-verifier
  loops with explicit rubrics (`APPROVED` / `REJECTED`). 10 regression tests in
  `tests/test_graph_engine.py`
- **Per-key × per-model scoring** — `KeyModelHealth` learns a composite score
  (success rate + median time to first token) from live traffic; candidate
  selection now prefers the best key for the requested model
- **Adaptive RPM ceiling** — each key's sliding window learns the real ceiling
  from `Retry-After` headers on 429s and never spends the full 28 RPM budget
  against a tighter upstream limit
- **Multi-endpoint NVIDIA routing** — extra upstream endpoints via
  `endpoints.json` or `OPENVIDIA_UPSTREAM_ENDPOINTS`; 60s blacklist on ≥500
  errors, automatic recovery on success
- **Multi-node sync via Redis (opt-in)** — cooldowns, invalid keys, pool
  throttling and model circuit breakers shared across instances over pub/sub
  (`redis_config.json` / `OPENVIDIA_REDIS_URL`, extra `openvidia[redis]`)
- **Embedding cache** — in-memory cache for `/v1/embeddings` (TTL 300s, SHA-256
  keyed by model + input); hit/miss counters on `/ops/health`
- **Free-tier provider fallback** — when every NVIDIA key is exhausted, the
  catch-all fails over to OpenAI-compatible providers declared in
  `providers.json` (unresolved keys skip the provider silently)
- **Ops endpoints** — `/ops/keys` and `/ops/health`, protected by the
  control-plane token, exposing pool, models, endpoints, embedding cache and
  Redis state
- Jcode as a first-class CLI target (`openvidia setup` configures it)
- **Smart key rotation** — rotation ordered by in-flight cost, per-key RPM and
  consecutive failures, so concurrent traffic spreads across the pool instead
  of piling onto key[0]
- **Network-error circuit breaker** — three consecutive network errors stop
  rotation (503) instead of burning every key; 504 timeouts cool the key for
  30s without consuming the attempt budget

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
- Key selection is model-aware: candidates are ordered by a per-key × per-model
  score learned from live traffic (see Added)
- The test suite ships 297 regression tests (proxy, shims, compaction, graph
  engine, delivery features)

### Removed
- **The pinned default model.** `DEFAULT_MODEL = "deepseek-ai/deepseek-v4-pro"`
  was the last hardcoded model name in the codebase — a liability the day the
  provider retires it, and a silent override of what you selected. The model
  is now resolved live: active selection → first starred preset → an error
  saying none is selected
- **Preset-based model fallback.** A request for a model that failed on every
  key was silently retried on the next starred model, so output could come
  from a model you did not choose without the response saying so. The selected
  model is now the only model a request runs on; when it fails you get a 503
  naming it. ★ Starred presets remain a quick-switch shortlist.

### Fixed
- **Closing the desktop window killed the proxy.** The window's `closed` event
  stopped the proxy even with a tray icon present, so every dashboard control
  went dead at once — Start, Stop and the model switch all POST to an API that
  was no longer answering. With a tray, the window is a view onto a background
  service: closing it hides the view
- **The Codex and Claude Code paths never joined the key load balancer.** Only
  the catch-all claimed a key before sending, so concurrent requests through
  the shims scored the whole pool at zero, tie-broke on index, and piled onto
  key[0] — a 26-key pool with 713 RPM of headroom producing 429s while 25 keys
  idled
- **502/503/504 no longer cool a key down.** A gateway timeout is the provider
  giving up on a slow model; every key hits the same wall, and charging it to
  the key that carried the request empties the pool one timeout at a time
- **The installer reported failure on a successful install.** It slept 3
  seconds and probed once, while startup pre-warms every key and takes tens of
  seconds. It now polls until healthy, gives up if the launcher dies, and
  exits non-zero with the command to see the error. It also no longer `pkill`s
  by pattern — the app frees its own port
- **A slow model took the whole key pool down with it.** The 30s upstream read
  timeout is the wait for the *first byte*, and a reasoning model emits nothing
  while it thinks (measured: `z-ai/glm-5.2` at 117-162s to first token, at any
  prompt size, while `deepseek-v4-flash` answered in 2.1s on the same keys in
  the same minute). Every request to such a model timed out on every key, and
  each timeout put a healthy key on cooldown. Timeouts now default to 240s,
  live in `timeouts.json`, and a read timeout no longer blames the key
- SSE keepalive comments during the wait, so a thinking model is
  distinguishable from a dead connection
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
