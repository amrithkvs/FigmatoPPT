#!/usr/bin/env bash
# Build "Figma Deck.app" — a standalone macOS app (no Python install needed).
set -e
cd "$(dirname "$0")"

# Build deps live in the venv but aren't needed at runtime.
.venv/bin/pip install -q pyinstaller pywebview

cd backend
../.venv/bin/pyinstaller --noconfirm --windowed --clean \
  --name "Figma Deck" \
  --distpath ../dist \
  --workpath ../build \
  --specpath ../build \
  --osx-bundle-identifier com.figmadeck.app \
  --add-data "../frontend/index.html:frontend" \
  --collect-submodules uvicorn \
  --collect-submodules apscheduler \
  --collect-submodules webview \
  --collect-data pptx \
  --collect-data webview \
  desktop.py

echo ""
echo "✅ Built: dist/Figma Deck.app"
echo "   Open it with: open 'dist/Figma Deck.app'"
echo "   (First launch on another Mac: right-click → Open to clear Gatekeeper.)"
