# Operations

Installation, configuration, API reference, and tuning for OpenVidia.

---

## Installation

### Linux (Ubuntu / Arch / Fedora)

```bash
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
./install.sh
```

Or manually:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv (recommended)
uv sync
uv run openvidia setup   # auto-configure detected CLIs
uv run openvidia          # start proxy + desktop app
```

With pip: `pip install -e . && openvidia setup && openvidia`

### macOS

```bash
brew install python@3.12 pygobject pkg-config
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
pip install -e .
openvidia setup
openvidia
```

pywebview on macOS uses system WebKit (native, no extra GTK deps needed).

### Windows

```cmd
git clone https://github.com/ciroautuori/openvidia.git
cd openvidia
pip install -e .
openvidia setup
openvidia
```

pywebview on Windows uses EdgeChromium (WebView2, pre-installed on Win 10/11).

### Optional: auto key regeneration

```bash
pip install -e ".[auto-regen]"
playwright install chromium
```

Enables the AccountManager to automatically generate replacement keys when a key dies (401/403). Uses CDP (primary) or Playwright (fallback). Without this, dead keys stay parked until manually replaced.

---

## CLI setup

`openvidia setup` auto-configures any detected CLI. The setup logic lives in `openvidia/__main__.py`:

### opencode (`_setup_opencode`)

Edits `~/.config/opencode/opencode.json`:
- Adds `openvidia` provider pointing at `http://localhost:1919/v1`
- Adds `openvidia` model with `tools: True`
- Enables auto-compaction (`auto: true, prune: true, reserved: 8000`)
- Sets default model + small model to `openvidia/openvidia`
- Adds `AGENTS.md` to instructions if present in the working directory

### Codex CLI (`_setup_codex`)

Edits `~/.codex/config.toml`:
- Sets `model = "openvidia"` and `model_provider = "openvidia"`
- Adds `[model_providers.openvidia]` block with `base_url`, `env_key`, `wire_api = "responses"`

Also adds `export OPENVIDIA_API_KEY=ignored` to the shell rc file (`.zshrc`, `.bashrc`, or fish config).

### Grok (`_setup_grok`)

Edits `~/.grok/config.toml`:
- Adds `[model.openvidia]` block with `base_url`, `api_key`, `api_backend`, `context_window`
- Sets `default = "openvidia"` in the `[models]` section

### Claude Code (manual)

Point Claude Code at the Anthropic Messages shim — no `setup` command needed:

```bash
export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored
claude --model openvidia
```

---

## Configuration

### Config directory

Cross-platform config paths defined in `openvidia/config.py`:

| Platform | Path |
|----------|------|
| **Linux** | `~/.config/openvidia/` (honors `XDG_CONFIG_HOME`) |
| **macOS** | `~/Library/Application Support/openvidia/` |
| **Windows** | `%APPDATA%\openvidia\` |

### Config files

| File | Purpose |
|------|---------|
| `keys.json` | API keys (JSON array of `nvapi-...` strings) |
| `presets.json` | ★ Starred models shortlist — also the fallback chain (ordered list) |
| `active_model` | Currently active model (persists across restarts) |
| `index` | Key rotation index |
| `compaction.json` | Auto-compaction tuning (optional — see below) |
| `accounts.json` | Legacy accounts for auto-regen (auto-extracted to `keys.json` if `keys.json` is empty) |
| `singleton.lock` | Singleton lock (prevents multiple proxy instances) |
| `stop` | Stop flag (sent by `POST /api/stop`, checked on startup) |

All file writes are atomic (write to `.tmp`, rename) via `config.atomic_write()`.

### Adding keys

Via the dashboard (**Keys** section), or edit `keys.json`:

```json
["nvapi-xxx", "nvapi-yyy", "..."]
```

If `keys.json` is empty on startup, `_extract_keys_from_accounts()` pulls keys from `accounts.json` automatically.

---

## API endpoints

### Proxy

| Method | Path | Description |
|--------|------|-------------|
| `*` | `/v1/{path}` | Catch-all forward to NVIDIA NIM (streaming supported, model override + compaction applied) |
| `POST` | `/v1/responses` | OpenAI Responses API shim → chat/completions (Codex CLI) |
| `POST` | `/v1/messages` | Anthropic Messages API shim → chat/completions (Claude Code) |
| `GET` | `/v1/models` | List available models from upstream (first healthy key) |
| `GET` | `/health` | Health check — key count, port, status |

### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/status` | Proxy running state, port, key count, cooldown count |
| `GET` | `/api/stats` | Requests, rotations, success, active index, cooldowns, total RPM |
| `GET` | `/api/keys/stats` | Per-key: requests, success/fail, cooldown, RPM, reason, freshness, is_valid |
| `GET` | `/api/keys` | List all keys |
| `POST` | `/api/keys` | Replace all keys |
| `POST` | `/api/keys/add` | Add a key |
| `POST` | `/api/keys/remove` | Remove a key (by index or value) |
| `GET/POST` | `/api/model` | Get/set active model override |
| `GET/POST` | `/api/presets` | Get/save starred model presets (fallback chain) |
| `POST` | `/api/test-model` | Test a model directly (bypasses override, tries each key) |
| `POST` | `/api/stop` | Stop proxy (returns 503 to clients until restarted) |
| `POST` | `/api/start` | Resume proxy |
| `POST` | `/api/restart` | Zero-downtime restart (spawn new process, kill old) |
| `GET` | `/api/logs/stream` | SSE real-time log stream |
| `GET/POST` | `/api/accounts` | Manage legacy accounts (auto-regen) |
| `POST` | `/api/accounts/active` | Set active account |

Routes are registered in `openvidia/webui.py` (`attach_webui`).

---

## Rate limit tuning

Constants in `openvidia/proxy_state.py`:

```python
MAX_RPM = 28              # Safe margin below NVIDIA's 40 RPM limit
RPM_WINDOW = 60.0         # Sliding window in seconds

COOLDOWN_DURATIONS = {
    400: 120.0,           # Bad request — model access issue
    401: 3600.0,          # Unauthorized — dead key (also marks is_valid=False)
    403: 3600.0,          # Forbidden — dead key (also marks is_valid=False)
    404: 120.0,           # Not found — model not on this key
    429: 60.0,            # Rate limited (Retry-After header overrides)
}
DEFAULT_COOLDOWN = 30.0   # Network errors, unknown 5xx
```

**Note:** 400 and 404 are returned to the client without rotation — they're deterministic on the payload, so rotating would waste keys. The cooldown values above are applied when these statuses come back during the preset-fallback retry path.

To tune, edit the constants in `proxy_state.py` and restart. There are no environment variables for rate-limit configuration.

---

## Bounded rotation tuning (July 2026)

Added to prevent the Codex CLI compaction/rotation block when the pool is
saturated. Constants in `proxy_app.py`, `responses_shim.py`, and `compaction.py`:

| Constant | File | Default | Meaning |
|----------|------|---------|---------|
| `_MAX_ROTATE_ATTEMPTS` | `proxy_app.py`, `responses_shim.py` | `5` | Hard cap on upstream sends per rotation phase (primary + per fallback model). |
| `_ROTATE_SEND_TIMEOUT` | `proxy_app.py`, `responses_shim.py` | `Timeout(connect=4, read=30, write=10, pool=30)` | Bounded per-attempt timeout — was 120s client default × up to 25 keys. |
| `_MIN_LIVE_FRACTION` | `proxy_app.py`, `responses_shim.py` | `0.2` | If live keys < `max(1, 20% × pool)` → skip rotation, go to fallback / 503. Weighs against the full pool size, not `len(candidates)`. |
| `count_live_candidates()` | `proxy_state.py` | — | `(live, valid)` probe used by saturation gates in both proxies. |

Compaction (lower-stakes) uses its own boundary values in `_DEFAULTS` inside
`compaction.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `summary_model` | `""` | Server-local "quiet" model — `""` means use `DEFAULT_MODEL` (single source of truth, no hardcoded provider). |
| `max_summarize_attempts` | `3` | Hard cap on summarize rotate attempts (was unbounded = 25). |
| `summarize_timeout` | `8.0` | Per-attempt connect+read+write+pool cap (was read=30s only). |
| `min_healthy_fraction` | `0.25` | If live keys < 25% of pool → deterministic `_trim()` immediately. No upstream sends. |

These `_DEFAULTS` carry the safe values; users get the fix even with no
`compaction.json`. Existing JSON merges cleanly (missing keys fall back to
`_DEFAULTS`).


---

## Auto-compaction tuning

Optional — create `~/.config/openvidia/compaction.json` (defaults shown):

```json
{
  "enabled": true,
  "budget_tokens": 80000,
  "keep_recent": 8,
  "summary_max_tokens": 1024,
  "reserved_tokens": 8000,
  "summary_model": "",
  "max_summarize_attempts": 3,
  "summarize_timeout": 8.0,
  "min_healthy_fraction": 0.25,
  "model_budgets": {}
}
```

| Field | Meaning |
|-------|---------|
| `enabled` | Turn compaction on/off |
| `budget_tokens` | Estimated history size that triggers compaction (~4 chars/token) |
| `keep_recent` | Most recent messages always kept verbatim |
| `summary_max_tokens` | Cap on the generated summary length |
| `reserved_tokens` | Generation headroom reserved from budget |
| `summary_model` | Server-local "quiet" model used for summarize (never `state.active_model`). `""` means use `DEFAULT_MODEL`. |
| `max_summarize_attempts` | Hard cap on summarize rotate attempts (was unbounded = up to 25). |
| `summarize_timeout` | Per-attempt connect+read+write+pool cap in seconds (was read=30s only). |
| `min_healthy_fraction` | If live keys < this fraction of the pool → deterministic `_trim()` immediately, no upstream sends. |
| `model_budgets` | Optional per-model context budget overrides (tokens). Keys are NVIDIA NIM model ids (e.g. `"z-ai/glm-5.2": 120000`). No model is hardcoded — the proxy is provider-agnostic. Falls back to `budget_tokens`. |

See [Architecture → Auto-compaction](architecture.md#auto-compaction--compactionpy) for how the summarize/trim strategy works.

---

## Desktop app backends

pywebview auto-detects the best available backend per platform:

| Backend | Platform | Engine |
|---------|----------|--------|
| **Qt WebEngine** | Linux (KDE/Wayland) | PyQt6-WebEngine (native, best experience) |
| **GTK WebKit** | Linux (GNOME/X11) | PyGObject + WebKitGTK |
| **WebKit** | macOS | system WebKit (no extra deps) |
| **EdgeChromium** | Windows | WebView2 (pre-installed on Win 10/11) |

### Linux desktop integration

`install.sh` installs a `.desktop` file and icon. To do it manually:

```bash
cp openvidia.desktop ~/.local/share/applications/
cp web/assets/logo.png ~/.local/share/icons/hicolor/256x256/apps/openvidia.png
update-desktop-database ~/.local/share/applications/
```

### System tray (Linux/Qt)

On Linux with the Qt backend, a `QSystemTrayIcon` provides Show/Quit actions. Closing the window hides it to the tray (proxy keeps running); "Quit" from the tray menu terminates the proxy. The tray is created via a cross-thread `pyqtSignal` so it runs on the Qt main loop.

---

## Server binding

The proxy binds **loopback only** on both IP stacks:

- IPv4: `127.0.0.1:1919`
- IPv6: `::1:1919` (best-effort — skipped if IPv6 is unavailable, doesn't fail startup)

This ensures `localhost` works whether the client's resolver picks IPv4 or IPv6 (Node/Bun try IPv6 first). The proxy never binds all-interfaces because it serves unauthenticated API keys. See `server_manager.py`.
