#!/usr/bin/env bash
set -e

# Lavora sempre dalla directory dello script
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "╔════════════════════════════════════════════╗"
echo "║   OpenVidia — Installazione automatica     ║"
echo "╚════════════════════════════════════════════╝"
echo ""

# ── 1. Dipendenze Python ──────────────────────────
echo "▶ Installa dipendenze Python..."
if command -v uv >/dev/null 2>&1; then
    uv sync --quiet
    echo "  ✓ Dipendenze installate (uv)"
elif command -v pip >/dev/null 2>&1; then
    pip install -e . --quiet
    echo "  ✓ Dipendenze installate (pip)"
else
    echo "  ✗ Devi installare uv o pip prima di continuare"
    exit 1
fi
echo ""

# ── 2. Launcher in ~/.local/bin ───────────────────
echo "▶ Installa launcher..."
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/openvidia" << 'LAUNCHER'
#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)/.."
# Prova prima con uv, fallback a python diretto
if command -v uv >/dev/null 2>&1; then
    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && echo "$(dirname "$HOME")")"
    PROJECT_DIR="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        : # ok
    else
        PROJECT_DIR="/home/$(whoami)/Scrivania/openvidia"
    fi
fi

pkill -f "python.*-m openvidia" 2>/dev/null || true
export PYTHONDONTWRITEBYTECODE=1

if command -v uv >/dev/null 2>&1 && [ -f "$PROJECT_DIR/pyproject.toml" ]; then
    nohup uv run --directory "$PROJECT_DIR" python -m openvidia "$@" > /dev/null 2>&1 &
else
    nohup python3 -m openvidia "$@" > /dev/null 2>&1 &
fi

echo "● OpenVidia avviato su http://localhost:1919"
LAUNCHER
chmod +x "$BIN_DIR/openvidia"
echo "  ✓ Launcher installato in $BIN_DIR/openvidia"
echo ""

# ── 3. Configura opencode ────────────────────────
echo "▶ Configura opencode..."
python3 -m openvidia setup
echo ""

# ── 4. Avvia e verifica ──────────────────────────
echo "▶ Verifica avvio..."
export PYTHONDONTWRITEBYTECODE=1
nohup python3 -m openvidia > /dev/null 2>&1 &
sleep 3
if curl -s http://localhost:1919/health >/dev/null 2>&1; then
    KEYS=$(curl -s http://localhost:1919/health | python3 -c "import sys,json; print(json.load(sys.stdin)['keys'])" 2>/dev/null || echo "?")
    echo "  ✓ Proxy attivo — $KEYS key caricate su http://localhost:1919"
else
    echo "  ⚠ Proxy non ancora attivo — controlla con: python3 -m openvidia foreground"
fi
echo ""

echo "╔════════════════════════════════════════════╗"
echo "║   Installazione completata!                ║"
echo "╠════════════════════════════════════════════╣"
echo "║   Proxy:    http://localhost:1919         ║"
echo "║   Provider: openvidia (auto-configurato)   ║"
echo "║   Compaction: auto + prune attivi         ║"
echo "║                                            ║"
echo "║   Usa in opencode:  /model openvidia       ║"
echo "║   Dashboard:        http://localhost:1919  ║"
echo "╚════════════════════════════════════════════╝"
