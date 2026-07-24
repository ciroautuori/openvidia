#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "╔════════════════════════════════════════════╗"
echo "║   OpenVidia — Automatic Installer          ║"
echo "╚════════════════════════════════════════════╝"
echo ""

# ── 1. Python dependencies ──────────────────────
echo "▶ Installing Python dependencies..."
# Whatever installs the dependencies also has to run the app: uv puts them in a
# local venv, so the system python3 would start with nothing importable.
if command -v uv >/dev/null 2>&1; then
    uv sync --quiet
    RUN=(uv run openvidia)
    echo "  ✓ Dependencies installed (uv)"
elif command -v pip >/dev/null 2>&1; then
    pip install -e . --quiet
    RUN=(python3 -m openvidia)
    echo "  ✓ Dependencies installed (pip)"
else
    echo "  ✗ You need to install uv (recommended) or pip before continuing"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo ""

# ── 2. Auto-configure detected CLIs ─────────────
echo "▶ Auto-configuring CLIs (opencode, Codex, Claude Code, Grok)..."
"${RUN[@]}" setup 2>/dev/null || true
echo ""

# ── 3. Desktop integration (Linux only) ────────
if [[ "$(uname -s)" == "Linux" ]]; then
    echo "▶ Installing desktop entry..."
    DESKTOP_DIR="$HOME/.local/share/applications"
    ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
    mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
    cp openvidia.desktop "$DESKTOP_DIR/" 2>/dev/null || true
    cp web/assets/logo.png "$ICON_DIR/openvidia.png" 2>/dev/null || true
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "  ✓ Desktop entry installed"
    echo ""
fi

# ── 4. Start and verify ─────────────────────────
echo "▶ Starting proxy + desktop app..."
# No pkill: it matches by pattern (so it can hit unrelated processes), it does
# not catch the launcher wrapper, and it never checks whether anything died.
# The app frees port 1919 itself on startup, escalating if the previous
# instance ignores the polite request.
nohup "${RUN[@]}" > /dev/null 2>&1 &
LAUNCHER=$!

# Poll instead of sleeping. Startup pre-warms every key, which takes tens of
# seconds on a large pool — a fixed `sleep 3` reported failure on a perfectly
# good install.
DEADLINE=$((SECONDS + 60))
READY=""
while [ $SECONDS -lt $DEADLINE ]; do
    if curl -s --max-time 3 http://localhost:1919/health >/dev/null 2>&1; then
        READY=1
        break
    fi
    if ! kill -0 "$LAUNCHER" 2>/dev/null; then
        break   # launcher exited — no point waiting out the deadline
    fi
    sleep 1
done

if [ -n "$READY" ]; then
    KEYS=$(curl -s --max-time 5 http://localhost:1919/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('keys','?'))" 2>/dev/null || echo "?")
    echo "  ✓ Proxy active — $KEYS keys on http://localhost:1919"
    echo "  ✓ Desktop app opened"
else
    echo "  ✗ Proxy did not come up. See the error with:"
    echo "      ${RUN[*]} foreground"
    exit 1
fi
echo ""

echo "╔════════════════════════════════════════════╗"
echo "║   Installation complete!                   ║"
echo "╠════════════════════════════════════════════╣"
echo "║   Command:    openvidia                    ║"
echo "║   Proxy:      http://localhost:1919/v1     ║"
echo "║   Dashboard:  http://localhost:1919        ║"
echo "╠════════════════════════════════════════════╣"
echo "║   opencode → /model openvidia              ║"
echo "║   codex    → codex --model openvidia       ║"
echo "║   grok     → grok --model openvidia        ║"
echo "╚════════════════════════════════════════════╝"
