# Changelog

All notable changes to OpenVidia will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Unit tests for proxy rotation, cooldown management, and compaction (46 tests)
- GitHub issue templates for bugs, features, enhancements, and questions
- CONTRIBUTING.md guide for new contributors
- SECURITY.md with vulnerability disclosure process
- Error logging improvements for better debugging

### Changed
- Version bumped from 2.0.0 to 1.0.0 (first stable release)
- Test suite uses pytest with async support

### Fixed
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
