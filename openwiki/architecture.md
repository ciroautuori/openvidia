# Architecture

OpenVidia is a single-process Python application: a FastAPI reverse proxy backed by httpx (HTTP/2), a desktop dashboard served from the same process, and an optional account manager that auto-regenerates dead keys.

## Process model

```
openvidia (CLI)
  └─ spawns: python -m openvidia foreground  (background subprocess)
      └─ asyncio event loop
          ├─ uvicorn server (:1919, IPv4 + IPv6 loopback)
          │     └─ FastAPI app (proxy_app.create_app)
          │           ├─ proxy routes (/v1/*)
          │           ├─ shim routes (/v1/responses, /v1/messages)
          │           └─ webui routes (/, /api/*)
          ├─ background health check task (every 30s)
          └─ AccountManager task (if [auto-regen] installed)

  └─ opens: pywebview native window → http://localhost:1919
```

The proxy runs as a background subprocess (`openvidia` command) or in the foreground (`openvidia foreground`). The desktop app is a pywebview window pointed at the same FastAPI server that serves the proxy.

---

## Proxy engine — `proxy_app.py`

`create_app(state, web_dir)` builds the FastAPI app. The upstream is `https://integrate.api.nvidia.com/v1/`. The app registers:

1. **`/v1/responses`** → `handle_responses` (Responses shim for Codex)
2. **`/v1/messages`** → `handle_anthropic_messages` (Anthropic shim for Claude Code)
3. **`/v1/models`** → queries upstream with the first healthy key, returns OpenAI-formatted model list
4. **`/v1/{full_path:path}`** → catch-all proxy handler (the main request path)

### Catch-all proxy flow (`proxy_handler`)

```
1.  Parse body as JSON (if present)
2.  Model override:
      - if model == "openvidia" → state.active_model or DEFAULT_MODEL
      - if state.active_model is set → override whatever the client sent
3.  Auto-compaction: if messages list exists and full_path ends with "chat/completions"
      → call compaction.maybe_compact() → may replace messages
4.  Key rotation: state.get_candidate_keys() → ordered list of (index, key)
5.  For each candidate key:
      a. Skip if RPM saturated (key_rpm >= 28)
      b. Build request with Authorization: Bearer <key>
      c. Send (streaming) to upstream
      d. On 2xx: record RPM, restore key, stream response back (client disconnect aware)
      e. On ReadTimeout: stop the phase — the upstream accepted the request
         and is still thinking, so the key is NOT cooled down and NOT rotated
         (the next key would run the same model and wait exactly as long)
      f. On HTTP error: mark_key_failed() (30s cooldown), rotate
      g. On should_rotate(status) [401/403/429/5xx]: mark_key_failed(), rotate
      h. On 400/404: return directly to client (no rotation — deterministic error)
6.  All keys exhausted → return the last upstream status to the client,
    naming the model. NEVER retried on a different model.
```

**Key insight — 400/404 are not rotated:** These errors are deterministic on the request payload (bad format, unknown model). Rotating would waste keys because every key gets the same error. The response is returned to the client immediately and the key is untouched. See `ROTATE_STATUSES` and `should_rotate()` in `proxy_app.py`.

### Health check

`_health_check_all` runs on startup (force=True) and every 30 seconds (`_background_health_check`). For each key on cooldown with < 90s remaining, it sends a lightweight `GET /v1/models` probe. If the key responds OK, the cooldown is cleared and the key is revived.

### No model substitution

The model the user selected is the only model a request runs on. Answering
from a different model when the selected one fails makes the proxy lie about
what produced the output — the response carries no marker saying it came from
elsewhere. When every key fails for a model, the error names that model.
`presets.json` is a quick-switch shortlist for the dashboard, nothing more.

---

## Shared state — `proxy_state.py`

`ProxyState` holds all mutable proxy state. It is the single source of truth shared between the proxy handler, the webui, and the account manager.

### Key state classes

| Class | Purpose |
|-------|---------|
| `KeyState` | Per-key: `is_valid` (permanent validity — false after 401/403), `cooldown_until`, `last_error`. Uses `__slots__`. |
| `KeyCooldown` | Dataclass: `until` (timestamp), `reason`. Properties `remaining` and `active`. |
| `RpmTracker` | Sliding-window RPM: deque of timestamps, pruned to last 60s. `can_send()` checks against `MAX_RPM` (28). |
| `KeyUsage` | Per-key stats: requests, success, failed, last_used, last_error. |
| `ProxyStats` | Global stats: requests, rotations, success, current_index, active_key_index, key_usage dict. |

### `get_candidate_keys()` — the rotation brain

This method (added in the most recent refactor) is the heart of key selection. It:

1. Partitions keys into **available** (valid + not on cooldown) and **cooldown** buckets
2. If no available keys but some on cooldown, sorts cooldown keys by remaining time and uses them (logged as a warning)
3. Rotates the available list starting from `current_index`
4. **Pre-claims** the next index immediately so concurrent requests don't collide on the same starting key

```python
# Pre-claim: advance index to next candidate immediately
next_candidate_idx = ordered[0][0]
self.stats.current_index = (next_candidate_idx + 1) % len(self._keys)
self.stats.active_key_index = next_candidate_idx
```

### Thread safety

Key list mutations come from two places: the asyncio event loop (webui, proxy) and OS threads (account_manager's key regeneration). An `asyncio.Lock` alone cannot serialize across threads, so `ProxyState` uses a `threading.Lock` (`_keys_write_lock`) around `_keys` and `_key_states` writes. The keys setter rebuilds the state dict atomically under the lock, preserving existing key states for keys that remain.

### Cooldown durations

Defined in `COOLDOWN_DURATIONS` (also documented in [Operations](operations.md#rate-limit-tuning)):

```python
COOLDOWN_DURATIONS = {
    400: 120.0,   # bad request — model access issue
    401: 3600.0,  # unauthorized — dead key (also marks is_valid=False)
    403: 3600.0,  # forbidden — dead key (also marks is_valid=False)
    404: 120.0,   # not found — model not on this key
    429: 180.0,   # rate limited (Retry-After header overrides this)
}
DEFAULT_COOLDOWN = 30.0  # network errors, unknown 5xx
```

`mark_key_failed(key, status, retry_after)` sets the cooldown and, for 401/403, permanently marks the key invalid (`is_valid = False`). It also calls `on_key_failed` callback if set — this hooks into the AccountManager for auto-regeneration.

---

## API shims

OpenVidia speaks three API dialects. The catch-all proxy handles OpenAI `chat/completions` natively. Two shims translate other formats to/from it.

### Responses shim — `responses_shim.py` (Codex CLI)

`handle_responses(request, state, client)` translates the OpenAI Responses API (`/v1/responses`) to `chat/completions`:

- **Request:** `input` (string or array of InputItems) → `messages[]`. Maps `role="developer"` → `system`, `type="input_text"` → text.
- **Response:** chat completion → Responses output items (text, function_call)
- **Streaming:** SSE chat chunks → SSE Responses events
- **Tools:** function definitions → chat tools and back
- **Sanitization:** `_sanitize_chat_messages()` ensures every message is valid for NVIDIA NIM — flattens content arrays to strings, synthesizes missing `tool_call_id`s, coerces `content: null` on assistant messages to `""` (NIM rejects null content).

### Anthropic shim — `anthropic_shim.py` (Claude Code)

`handle_anthropic_messages(request, state, client)` translates Anthropic Messages API (`/v1/messages`) to `chat/completions`:

- **Request:** `system` (separate field or array of text blocks) → first system message. Content blocks (`text`, `tool_use`, `tool_result`, `image`) → chat messages.
- **Image handling:** NVIDIA NIM models are text-only. Image blocks are replaced with `[immagine omessa: il modello non supporta vision]` (and `[image omitted: model has no vision]` in the translated path) so the model stays aware of the omission. A warning is logged.
- **Response:** chat completion → Anthropic content blocks (text, tool_use)
- **Streaming:** SSE chat chunks → SSE Anthropic events (`message_start`, `content_block_delta`, `message_stop`, etc.)
- **Sanitization:** same defensive `_sanitize_chat_messages()` as the Responses shim, shared logic for ensuring NIM-compatible payloads.

Both shims use `_CLIENT_ERR = {400, 404}`: these statuses are returned directly to the client without rotation (same logic as the catch-all proxy).

---

## Auto-compaction — `compaction.py`

Long conversations exceed the model's context window. Without handling, upstream returns `400` — and since that's deterministic on every key, a naive proxy would burn the entire pool before dying. OpenVidia **never blocks on context overflow**.

### Strategy

```
maybe_compact(messages):
  1.  If disabled or < 4 messages → return as-is
  2.  estimate_tokens(messages) <= budget? → return as-is (no compaction needed)
  3.  Split into (system_block, rest)
  4.  If rest <= keep_recent + 1 → nothing to compact
  5.  Cache hit? → serve [system_block, summary_block] + EVERY later message
      verbatim while that fits the budget. Steady state: zero upstream calls.
  6.  Otherwise the summary boundary must advance: keep the largest recent
      suffix that fits compact_ratio × budget, summarize everything before it
      on top of the previous summary (incremental, never the whole history)
  7.  Launch the summarize; concurrent requests on the same conversation share
      the one task. Wait at most inline_deadline for it
  8.  Still running at the deadline → serve now, the summarize continues
      detached and lands in the cache for the next turn
  9.  Fallback ladder: fresh summary → cached summary + verbatim remainder →
      deterministic _trim()
```

### Roll-forward cache

The `_rolling` dict (capped at 256 entries, FIFO eviction) maps `conv_key → (covered_count, summary_text, fingerprint_of_covered_prefix)`. Only messages **new** to that conversation are ever summarized. Two invariants make the hit possible, and both were once broken: `conv_key` must NOT include the message count (that minted a new key every turn), and the stored fingerprint must cover exactly `old[:covered]` (comparing it against the current, longer prefix never matched). With either one wrong the cache silently degrades to a full re-summarize per turn.

`_conv_key` uses the first 4 messages of `rest` (not just 1) as the conversation identity, specifically to avoid collisions with automated tools (Codex, opencode) that send identical initial prompts.

### Trim fallback

`_trim(system_block, rest, budget, keep_recent)` is deterministic and never fails: keeps system messages + first message + as many recent messages as fit the budget, inserting a `[previous messages omitted to fit context]` notice. Zero upstream cost. **Guarantee: the returned list never exceeds `budget` tokens** — even pathological inputs (a single message larger than the budget) are trimmed to a fragment that fits, so the upstream never 400s on context overflow. Per-model context budgets are NOT hardcoded: they come from the user `compaction.json` `model_budgets` map (falling back to `budget_tokens`); the proxy stays provider-agnostic.

### Hook points

Compaction is invoked in `proxy_app.py` for `chat/completions` requests, and in `anthropic_shim.py` for Claude Code messages. The Responses shim path also benefits since it routes through `chat/completions`. Config is optional (`~/.config/openvidia/compaction.json`) — see [Operations](operations.md#auto-compaction-tuning).

---

## Bounded rotation & saturation fast-fail (July 2026)

The serial-history Codex block is fixed by three coupled mechanisms:

1. **Bounded rotation attempts.** Every rotation phase (catch-all `proxy_app.py`,
   Responses shim stream + non-stream)
   is now capped at `_MAX_ROTATE_ATTEMPTS = 5`. The `get_candidate_keys()` ordering
   already puts the healthiest key first, so the first 5 attempts are the best
   5 candidates — no need to serially probe all 25.

2. **Per-attempt bounded timeout.** Each `client.send(...)` now passes
   `_ROTATE_SEND_TIMEOUT`, built from `config.upstream_timeouts()` (default
   `read=240s`, overridable in `timeouts.json`). The read budget is the wait
   for the FIRST byte: a reasoning model emits nothing while thinking
   (measured 117-162s for one model while another answered in 2.1s on the
   same keys), so a short ceiling makes it fail on every key. A ReadTimeout
   ends the phase without cooling the key down.

3. **Saturation gate.** Before any send, each loop computes a live snapshot
   (`live / total_pool`). If `live < max(1, int(total_pool * _MIN_LIVE_FRACTION))`
   with `_MIN_LIVE_FRACTION = 0.2`, the loop skips rotation entirely and goes
   straight to 503 + failure event. Crucially the denominator is
   the **full pool size** (`len(state.keys)`) — not `len(candidates)`, which would
   post-filter to the few cooldown-free keys and never trip the gate when the
   pool is actually saturated.

Hook points:

- `proxy_state.ProxyState.count_live_candidates()` — shared `(live, valid)` probe.
- `proxy_app.py` in `proxy_handler()` — gate + cap before the for-loop.
- `responses_shim.py` `_rotation_phase()` helper — shared bounded send loop
  (stream + non-stream).
- `responses_shim.py` `_keepalive_until()` — emits SSE comments while the
  upstream thinks, so a client can tell a slow model from a dead socket.
- `compaction.py` `maybe_compact()` — `min_healthy_fraction` gate (separate,
  lower-stakes path: a saturated summarize degrades to deterministic trim).

### Saturation gate: why total_pool, not len(candidates)

`get_candidate_keys()` returns `(available, cooldown_tail)` and further filters
on `is_valid` and cooldown. A saturated pool yields a short candidates list of
only the few recovered keys — `len(candidates)` is small by construction, so a
gate keyed on `live / len(candidates)` would almost never fire. The denominator
must be `len(state.keys)` so the fixture reflects "X out of 25 keys are actually
usable right now," which is the signal we want.

### Key-pool algorithm (SOTA, homogeneous free-tier, 25 keys)

1. **Single flat pool, no tiering.** Since all 25 NVIDIA NIM keys are the same
   free tier (~40 RPM each), there is no per-tier queue. One weighted pool.
2. **Weighted least-loaded selection** (`best_key_index`, `get_candidate_keys`):
   `cost = in_flight*4 + recent_rpm + consecutive_failures*8`. Concurrency bursts
   diverge across the pool (max-min fairness) rather than slamming one key.
3. **Adaptive RPM ceiling** per key: a 429 lowers the ceiling (floor at
   `ADAPTIVE_FLOOR_RPM`); a success raises it by `ADAPTIVE_REHAB_STEP` toward
   `MAX_RPM`. Prevents the post-cooldown spike that re-triggers 429.
4. **Respect Retry-After** headers on 429, replacing the default 180s cooldown.
5. **Background health probe** every 30s for cooldown keys with <90s remaining
   — revives recovered keys instead of waiting out the full window.
6. **Decay-only warm task** — decays idle RPM counters so the load balancer
   never sees artificially inflated costs after a quiet period.
7. **Bounded rotation + saturation gate** (this section) — the backstop that
   stops a saturated pool from blocking Codex CLI for minutes.

### Throughput math (queueing theory, M/M/c)

With `c = 25` keys at `μ = 28 RPM` and arrival rate λ (requests/min), pool
utilization `ρ = λ / (c·μ)`. The system is stable at `ρ < 1` → up to
`λ = 25 × 28 = 700 RPM`. Above that, queueing delay grows unbounded (Little: 
`L = λW`). Backoff after a fail uses jittered exponential `T_n = min(T_max,
T_0 · 2^n + jitter)`; jitter prevents the thundering-herd that recreates the
spike which caused the rate limit in the first place.

## Desktop app & webui

### Window — `__main__.py`

`open_desk(port)` creates a pywebview window (310×570 px, portrait phone-like) pointed at `http://localhost:1919`. pywebview auto-detects the best backend per platform (Qt WebEngine on Linux, WebKit on macOS, EdgeChromium on Windows).

On Linux with Qt, a `QSystemTrayIcon` is created via a cross-thread signal (`pyqtSignal` + `QueuedConnection`) so the tray lives on the Qt main loop. Close-to-tray hides the window instead of killing the proxy; a separate "Quit" tray action terminates the proxy via `_kill_proxy_by_port(port)` using psutil.

If pywebview is not installed, `webui.auto_open(port)` falls back to opening the system browser.

### Dashboard API — `webui.py`

`attach_webui(app, state, web_dir)` registers all non-proxy routes: static file serving (`/`, `/styles.css`, `/main.js`, `/logo.png`, `/favicon.ico`), `/health`, and the full `/api/*` surface (keys, model, presets, stats, logs, stop/start/restart, test-model, accounts). See the [API reference](operations.md#api-endpoints).

The frontend is vanilla HTML/CSS/JS in `web/` — no build step, no node_modules. The SSE log stream (`/api/logs/stream`) pushes real-time log lines from `ProxyState.log_buffer` to connected dashboard clients.

---

## Server binding — `server_manager.py`

`start()` binds the uvicorn server on **loopback only** (never all-interfaces — the proxy serves unauthenticated API keys). It binds both IPv4 (`127.0.0.1`) and IPv6 (`::1`) so clients resolving `localhost` to either stack work. IPv6 binding is best-effort: if the host has IPv6 disabled, that stack is skipped without failing startup. The app instance is created once and served on both sockets.

---

## Account manager & key regeneration

### `account_manager.py`

`AccountManager` hooks into `ProxyState.on_key_failed`. When a key gets a 401/403 (dead key), it:

1. Looks up the owning `Account` via `_key_owner` map
2. Spawns key regeneration (guarded by `_replenishing` set to prevent double-spawn)
3. Swaps the new key into the proxy pool transparently via `state.keys`

### `cdp_keygen.py` (primary)

Connects to a running Chrome instance via Chrome DevTools Protocol (reads `DevToolsActivePort`). Finds authenticated `build.nvidia.com` tabs, clicks through the API key generation modal, and extracts the new `nvapi-` key. Works with Google OAuth accounts — no password/cookie automation needed.

### `key_factory.py` (fallback)

Playwright-based headless Chromium automation. Two auth modes: `login_generate_key(email, password)` for credential-based accounts, and `generate_key(cookie_json)` for cookie-based legacy accounts. Navigates to `build.nvidia.com/settings/api-keys`, generates a key, optionally deletes the old one.

Auto-regeneration requires the `[auto-regen]` optional dependencies (`pip install -e ".[auto-regen]"` + `playwright install chromium`). Without it, dead keys stay parked until manually replaced.
