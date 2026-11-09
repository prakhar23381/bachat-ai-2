#!/bin/bash
# Starts the API (http://localhost:8000) and the web UI (http://localhost:5173).
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "[!] Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
if [ ! -d frontend/node_modules ]; then
    echo "[*] Installing frontend dependencies..."
    (cd frontend && npm ci)
fi
if [ -z "$BACHAT_KEY" ]; then
    echo "[!] BACHAT_KEY is not set: falling back to the public legacy key (no real encryption)."
fi

PYTHONPATH=. .venv/bin/uvicorn api.server:app --reload --port 8000 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null' EXIT

cd frontend && npm run dev
