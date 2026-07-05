#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Revert_to_Chrome.command
# Restores the Chrome-based launcher backup (created by Install_NativeApp.command)
# ─────────────────────────────────────────────────────────────────────────────

RES="$(cd "$(dirname "$0")" && pwd)"
MACOS="$RES/../MacOS"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   TelegramManager — Revert to Chrome ║"
echo "╚══════════════════════════════════════╝"
echo ""

BACKUP="$MACOS/launcher_chrome_backup"
TARGET="$MACOS/launcher"

if [ ! -f "$BACKUP" ]; then
    echo "❌  No backup found at:"
    echo "    $BACKUP"
    echo ""
    echo "    Run Install_NativeApp.command first to create a backup."
    echo ""
    read -p "Press Enter to close…"
    exit 1
fi

echo "→  Restoring launcher from backup…"
cp "$BACKUP" "$TARGET"
chmod +x "$TARGET"

if [ -f "$MACOS/launcher_chrome.sh" ]; then
    echo "→  Restoring launcher.sh…"
    cp "$MACOS/launcher_chrome.sh" "$MACOS/launcher.sh"
fi

# Re-sign
codesign --force --deep --sign - "$RES/../../" 2>/dev/null || true

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   ✅  Reverted to Chrome mode!       ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "TelegramManager.app will open in Chrome --app mode again."
echo ""
read -p "Press Enter to close…"
