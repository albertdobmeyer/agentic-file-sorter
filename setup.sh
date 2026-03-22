#!/usr/bin/env bash
set -e

# ─────────────────────────────────────────────
# AFS Setup
# ─────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { printf "  ${GREEN}[OK]${NC}   %s\n" "$1"; }
warn() { printf "  ${YELLOW}[WARN]${NC} %s\n" "$1"; }
fail() { printf "  ${RED}[FAIL]${NC} %s\n" "$1"; }

echo ""
echo "============================================"
echo "  AFS Setup"
echo "============================================"
echo ""

ERRORS=0

# --- Python 3.10+ -----------------------------------------------------------

if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    fail "Python not found on PATH"
    ERRORS=$((ERRORS + 1))
    PY=""
fi

if [ -n "$PY" ]; then
    PY_VERSION=$($PY --version 2>&1 | awk '{print $2}')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        ok "Python $PY_VERSION"
    else
        fail "Python $PY_VERSION found — 3.10+ required"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- pip ---------------------------------------------------------------------

if command -v pip3 &>/dev/null; then
    PIP=pip3
elif command -v pip &>/dev/null; then
    PIP=pip
elif [ -n "$PY" ] && $PY -m pip --version &>/dev/null; then
    PIP="$PY -m pip"
else
    fail "pip not found on PATH"
    ERRORS=$((ERRORS + 1))
    PIP=""
fi

if [ -n "$PIP" ]; then
    ok "pip available ($($PIP --version 2>&1 | awk '{print $2}'))"
fi

# --- Install requirements ----------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

if [ -n "$PIP" ] && [ -f "$REQ_FILE" ]; then
    echo ""
    echo "  Installing dependencies..."
    $PIP install -r "$REQ_FILE" --quiet 2>&1
    ok "pip install -r requirements.txt"
else
    if [ ! -f "$REQ_FILE" ]; then
        fail "requirements.txt not found at $REQ_FILE"
        ERRORS=$((ERRORS + 1))
    fi
fi

echo ""

# --- Ollama ------------------------------------------------------------------

if command -v ollama &>/dev/null; then
    ok "Ollama installed ($(ollama --version 2>&1 | head -1))"
else
    warn "Ollama binary not found on PATH"
fi

OLLAMA_URL="http://localhost:11434"

if curl -sf "$OLLAMA_URL/api/tags" &>/dev/null; then
    ok "Ollama is running at $OLLAMA_URL"

    TAGS=$(curl -sf "$OLLAMA_URL/api/tags")

    # Check vision model (llava)
    if echo "$TAGS" | grep -qi "llava"; then
        ok "Vision model (llava) is pulled"
    else
        warn "Vision model (llava) not found — run: ollama pull llava"
    fi

    # Check reasoning model (qwen3)
    if echo "$TAGS" | grep -qi "qwen3"; then
        ok "Reasoning model (qwen3) is pulled"
    else
        warn "Reasoning model (qwen3) not found — run: ollama pull qwen3:8b"
    fi
else
    warn "Ollama is not running at $OLLAMA_URL — start it with: ollama serve"
fi

# --- ffmpeg ------------------------------------------------------------------

if command -v ffmpeg &>/dev/null; then
    FFMPEG_VERSION=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')
    ok "ffmpeg $FFMPEG_VERSION"
else
    warn "ffmpeg not found on PATH — video preview extraction will be unavailable"
fi

# --- Summary -----------------------------------------------------------------

echo ""
echo "--------------------------------------------"

if [ "$ERRORS" -gt 0 ]; then
    fail "$ERRORS critical issue(s) detected"
else
    ok "Setup complete — no critical issues"
fi

echo ""
echo "  Run 'python afs.py status' to verify configuration."
echo ""
