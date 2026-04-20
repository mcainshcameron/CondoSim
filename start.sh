#!/usr/bin/env bash
# Condominio — start both backend and frontend on macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
else
  source .venv/bin/activate
fi

if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm install)
fi

(cd frontend && npm run dev) &
FRONTEND_PID=$!
trap "kill $FRONTEND_PID" EXIT

python -m backend.main
