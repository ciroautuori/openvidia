# OpenVidia — Multi-key proxy for NVIDIA NIM

## Mission
Proxy multi-chiave per NVIDIA NIM con dashboard desktop nativa. Pool di API key free-tier dietro un singolo endpoint con rotazione automatica, cooldown per-key, RPM limiting sliding-window, e auto-compaction.

---

## Architettura

```
Client CLI → localhost:1919/v1 → Proxy Engine → integrate.api.nvidia.com/v1
                                    │
                                    ├─ /v1/chat/completions  (catch-all: opencode, Grok, qualsiasi client OpenAI)
                                    ├─ /v1/responses         (shim: Codex CLI)
                                    ├─ /v1/messages          (shim: Claude Code)
                                    └─ /v1/models            (lista modelli upstream)
```

### Moduli core

| File | Righe | Ruolo |
|------|-------|-------|
| `proxy_app.py` | ~573 | FastAPI app factory, routing, health check, pre-warm |
| `proxy_state.py` | ~730 | Stato thread-safe: KeyState, cooldown, RPM tracker, circuit breaker |
| `responses_shim.py` | ~1126 | Shim `/v1/responses` → `/v1/chat/completions` (Codex CLI) |
| `anthropic_shim.py` | ~705 | Shim `/v1/messages` → `/v1/chat/completions` (Claude Code) |
| `compaction.py` | ~784 | Auto-compaction contesto per context overflow |
| `config.py` | ~351 | Path config cross-platform, timeout, model options |
| `__main__.py` | ~727 | Entry point, setup CLI (opencode/codex/grok), tray, server manager |
| `webui.py` | ~472 | Dashboard web (pywebview), API endpoints |
| `server_manager.py` | 107 | Avvio/stop uvicorn, binding dual-stack |
| `safe_file.py` | 197 | Backup atomico file di config |
| `_upstream_utils.py` | 43 | Semaphore globale + detection ResourceExhausted |

### Rotazione chiavi condivisa

`_rotation_phase()` in `responses_shim.py` è la funzione condivisa per tutti i percorsi:
- max 5 tentativi per fase, 3 fasi con 1s di pausa
- saturation gate: se <5% chiavi live, fast-fail con 503
- probe timeout (90s) sul primo tentativo per detection dead-model veloce
- send timeout (180s) sui tentativi successivi
- gestisce 429 ResourceExhausted (transient, key untouched) vs 429 rate-limit (cooldown)
- importata da `proxy_app.py` (catch-all), `anthropic_shim.py` (Claude Code)

### Costanti unificate

- `_MAX_ROTATE_ATTEMPTS`, `_ROTATE_SEND_TIMEOUT`, `_MODEL_PROBE_TIMEOUT`, `_MIN_LIVE_FRACTION` → definite in `responses_shim.py`, importate ovunque
- `UPSTREAM_BASE`, `UPSTREAM_CHAT` → definite in `config.py`, importate in `anthropic_shim.py`, `compaction.py`, `proxy_app.py`, `webui.py`

---

## CLI setup

```bash
openvidia setup    # configura opencode + Codex + Grok + Jcode automaticamente
openvidia          # avvia proxy + dashboard desktop
```

Claude Code richiede env vars manuali (auto-setup disabilitato per non mutare shell rc):
```bash
export ANTHROPIC_BASE_URL=http://localhost:1919
export ANTHROPIC_API_KEY=ignored
```

---

## Sviluppo

```bash
pip install -e .
pytest tests/ -v          # 91 test
ruff check openvidia/ tests/
ruff format --check openvidia/ tests/
```

### Config files (`~/.config/openvidia/`)

| File | Scopo |
|------|-------|
| `keys.json` | API keys (JSON array) |
| `presets.json` | Modelli starred (quick-switch) |
| `active_model` | Modello attivo (persiste tra restart) |
| `index` | Indice rotazione chiavi |
| `compaction.json` | Tuning auto-compaction (opzionale) |
| `timeouts.json` | Timeout upstream (opzionale) |
| `model_limits.json` | Context windows apprese dal proxy (non editare a mano) |
| `model_options.json` | Toggle reasoning + payload per-modello |
