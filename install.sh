#!/usr/bin/env bash
set -e

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
    echo "  ✗ Devi installare uv (consigliato) o pip prima di continuare"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
echo ""

# ── 2. Auto-configura tutte le CLI trovate ────────
echo "▶ Auto-configura CLI (opencode, Codex, Grok)..."
if command -v uv >/dev/null 2>&1; then
    uv run openvidia setup 2>/dev/null || true
elif command -v openvidia >/dev/null 2>&1; then
    openvidia setup 2>/dev/null || true
else
    python3 -m openvidia setup 2>/dev/null || true
fi
echo ""

# ── 3. Avvia e verifica ──────────────────────────
echo "▶ Avvia proxy + desktop app..."
pkill -f "python.*-m openvidia" 2>/dev/null || true
nohup python3 -m openvidia > /dev/null 2>&1 &
sleep 3
if curl -s http://localhost:1919/health >/dev/null 2>&1; then
    KEYS=$(curl -s http://localhost:1919/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('keys','?'))" 2>/dev/null || echo "?")
    echo "  ✓ Proxy attivo — $KEYS keys su http://localhost:1919"
    echo "  ✓ Desktop app aperta"
else
    echo "  ⚠ Proxy non ancora attivo — controlla: python3 -m openvidia foreground"
fi
echo ""

echo "╔════════════════════════════════════════════╗"
echo "║   Installazione completata!                ║"
echo "╠════════════════════════════════════════════╣"
echo "║   Comando:    openvidia                    ║"
echo "║   Proxy:      http://localhost:1919/v1     ║"
echo "║   Dashboard:  http://localhost:1919        ║"
echo "╠════════════════════════════════════════════╣"
echo "║   opencode → /model openvidia              ║"
echo "║   codex    → codex --model openvidia       ║"
echo "║   grok     → grok --model openvidia        ║"
echo "╚════════════════════════════════════════════╝"
