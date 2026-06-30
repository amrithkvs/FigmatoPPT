#!/usr/bin/env bash
# Start the Figma → Deck app on http://localhost:8000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r backend/requirements.txt
fi
# Load Microsoft cloud-sync credentials (and any overrides) if present.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
cd backend
../.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload
