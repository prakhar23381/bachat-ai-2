#!/bin/bash
echo "[*] Starting Bachat AI Machine Learning Engine..."

# Environment Setup
if [ ! -d ".venv" ]; then
    echo "[!] .venv not found. Please run 'python3 -m venv .venv' and 'pip install -r requirements.txt'"
    exit 1
fi

source .venv/bin/activate
export PYTHONPATH=.

# Start Python API Native Server
echo "[*] Booting FastAPI Instance at http://localhost:8000"
uvicorn api.server:app --reload --port 8000
