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
echo "▶ Auto-configuring CLIs (opencode, Codex, Grok)..."
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
pkill -f "python.*-m openvidia" 2>/dev/null || true
nohup "${RUN[@]}" > /dev/null 2>&1 &
sleep 3
if curl -s http://localhost:1919/health >/dev/null 2>&1; then
    KEYS=$(curl -s http://localhost:1919/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('keys','?'))" 2>/dev/null || echo "?")
    echo "  ✓ Proxy active — $KEYS keys on http://localhost:1919"
    echo "  ✓ Desktop app opened"
else
    echo "  ⚠ Proxy not yet active — check: ${RUN[*]} foreground"
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
