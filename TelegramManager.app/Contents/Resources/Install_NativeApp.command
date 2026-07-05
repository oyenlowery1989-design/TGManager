#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Install_NativeApp.command
#
# Run this ONCE to do the initial Swift compile.
# After that, future updates to launcher.swift compile automatically
# the next time you open TelegramManager.app — no manual step needed.
# ─────────────────────────────────────────────────────────────────────────────

RES="$(cd "$(dirname "$0")" && pwd)"
MACOS="$RES/../MacOS"
SWIFT_SRC="$RES/launcher.swift"
SWIFT_BIN="$MACOS/launcher_swift"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   TelegramManager — First-time Setup ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Check swiftc ──────────────────────────────────────────────────────────────
if ! command -v swiftc &>/dev/null; then
    echo "❌  swiftc not found."
    echo "    Install Xcode Command Line Tools:"
    echo "    xcode-select --install"
    echo ""
    echo "    Or just open TelegramManager.app — it will compile"
    echo "    automatically on first launch once tools are installed."
    echo ""
    read -p "Press Enter to close…"
    exit 1
fi

echo "✔  Swift: $(swiftc --version 2>&1 | head -1)"
echo ""
echo "→  Compiling native window (~20 seconds)…"
echo ""

swiftc "$SWIFT_SRC" \
    -o "${SWIFT_BIN}.tmp" \
    -framework Cocoa \
    -framework WebKit \
    -framework Foundation \
    -O

if [ $? -ne 0 ]; then
    echo ""
    echo "❌  Compilation failed."
    echo ""
    read -p "Press Enter to close…"
    exit 1
fi

mv "${SWIFT_BIN}.tmp" "$SWIFT_BIN"
chmod +x "$SWIFT_BIN"

echo "→  Re-signing app bundle…"
codesign --force --deep --sign - "$RES/../../" 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ✅  Done!                          ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "Double-click TelegramManager.app to launch."
echo ""
echo "ℹ  Future updates compile automatically on app open."
echo "   You never need to run this script again."
echo ""
read -p "Press Enter to close…"
