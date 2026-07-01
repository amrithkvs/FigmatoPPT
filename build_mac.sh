#!/usr/bin/env bash
# Build "FigPoint.app" — a standalone macOS app (no Python install needed).
set -e
cd "$(dirname "$0")"

# Build deps live in the venv but aren't needed at runtime.
.venv/bin/pip install -q pyinstaller pywebview

cd backend
../.venv/bin/pyinstaller --noconfirm --windowed --clean \
  --name "FigPoint" \
  --distpath ../dist \
  --workpath ../build \
  --specpath ../build \
  --osx-bundle-identifier com.figmadeck.app \
  --add-data "../frontend/index.html:frontend" \
  --add-data "../PowerPoint.svg:." \
  --add-data "../Figma-logo.svg.webp:." \
  --add-data "../Microsoft-logo.svg:." \
  --collect-submodules uvicorn \
  --collect-submodules apscheduler \
  --collect-submodules webview \
  --collect-data pptx \
  --collect-data webview \
  desktop.py

echo ""
echo "✅ Built: dist/FigPoint.app"
echo "   Open it with: open 'dist/FigPoint.app'"
echo "   (First launch on another Mac: right-click → Open to clear Gatekeeper.)"
