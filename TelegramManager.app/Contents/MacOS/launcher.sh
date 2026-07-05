#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# launcher.sh — Smart launcher for Telegram Manager
#
# Strategy:
#   1. Auto-compile launcher.swift into launcher_swift if source is newer
#   2. Run the native Swift app (WKWebView, menu bar)
#   3. Fallback to Chrome --app mode if Swift is unavailable
# ─────────────────────────────────────────────────────────────────────────────

MACOS_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCES="$(cd "$MACOS_DIR/../Resources" && pwd)"
SWIFT_SRC="$RESOURCES/launcher.swift"
SWIFT_BIN="$MACOS_DIR/launcher_swift"
SERVER_PY="$RESOURCES/server.py"
PARENT="$(cd "$MACOS_DIR/../.." && pwd)"

# ── Read port from manager_config.json (default 8477) ────────────────────────
PORT=8477
_CONFIG="$MACOS_DIR/../../manager_config.json"
if [ -f "$_CONFIG" ]; then
    _p=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('port',8477))" "$_CONFIG" 2>/dev/null)
    [ -n "$_p" ] && PORT=$_p
fi

SESSION_TOKEN="${TG_SESSION_TOKEN:-}"
if [ -z "$SESSION_TOKEN" ]; then
    SESSION_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null)
fi
if [ -z "$SESSION_TOKEN" ]; then
    SESSION_TOKEN=$(uuidgen 2>/dev/null | tr -d '-')
fi
export TG_SESSION_TOKEN="$SESSION_TOKEN"

# ── Best-effort cleanup of an older server for THIS bundle only ──────────────
# Match the absolute Resources/server.py path so we never kill a server that
# belongs to a different copy of the app installed elsewhere.
OLD_SERVER_PIDS=$(pgrep -f "$SERVER_PY" 2>/dev/null || true)
if [ -n "$OLD_SERVER_PIDS" ]; then
    kill $OLD_SERVER_PIDS 2>/dev/null || true
    sleep 0.2
fi

# ── Auto-compile Swift binary if source has changed ──────────────────────────
needs_compile=false

if [ ! -f "$SWIFT_BIN" ]; then
    needs_compile=true
elif [ "$SWIFT_SRC" -nt "$SWIFT_BIN" ]; then
    needs_compile=true
fi

COMPILE_LOG="$PARENT/data/launcher_compile.log"
[ -d "$PARENT/data" ] || COMPILE_LOG="$PARENT/launcher_compile.log"

if $needs_compile && command -v swiftc &>/dev/null; then
    # Notify user (non-blocking)
    osascript -e 'display notification "Compiling native window (first time, ~20s)…" with title "Telegram Manager"' 2>/dev/null &

    # Capture stderr to a log so compile failures are diagnosable, never masked.
    swiftc "$SWIFT_SRC" \
        -o "${SWIFT_BIN}.tmp" \
        -framework Cocoa \
        -framework WebKit \
        -framework Foundation \
        -O 2>"$COMPILE_LOG"
    compile_rc=$?

    if [ $compile_rc -eq 0 ]; then
        mv "${SWIFT_BIN}.tmp" "$SWIFT_BIN"
        chmod +x "$SWIFT_BIN"
        # Re-sign the app bundle
        codesign --force --deep --sign - "$MACOS_DIR/../../" 2>/dev/null || true
        osascript -e 'display notification "Native window ready!" with title "Telegram Manager"' 2>/dev/null &
    else
        rm -f "${SWIFT_BIN}.tmp"
        # If we have no usable binary, or the source is newer than the existing
        # one, the user is about to run stale/no native code — surface it.
        if [ ! -f "$SWIFT_BIN" ] || [ "$SWIFT_SRC" -nt "$SWIFT_BIN" ]; then
            osascript -e 'display alert "Launcher compile failed" message "Running the previous version. See data/launcher_compile.log for details."' 2>/dev/null || true
        fi
    fi
fi

# ── Launch native Swift app ───────────────────────────────────────────────────
if [ -f "$SWIFT_BIN" ]; then
    exec "$SWIFT_BIN"
fi

# ── Fallback: Chrome / Brave / Edge --app mode ───────────────────────────────
URL="http://127.0.0.1:$PORT/$SESSION_TOKEN/"
PROFILE="/tmp/TelegramManagerApp"

python3 "$RESOURCES/server.py" &
SERVER_PID=$!

for i in $(seq 1 30); do
    curl -s "$URL" > /dev/null 2>&1 && break
    sleep 0.2
done

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

LAUNCH_ARGS=(--app="$URL" --window-size=1100,750 --user-data-dir="$PROFILE"
             --no-first-run --no-default-browser-check --disable-extensions --disable-sync)

if   [ -f "$CHROME" ]; then "$CHROME" "${LAUNCH_ARGS[@]}" 2>/dev/null &
elif [ -f "$BRAVE"  ]; then "$BRAVE"  "${LAUNCH_ARGS[@]}" 2>/dev/null &
elif [ -f "$EDGE"   ]; then "$EDGE"   "${LAUNCH_ARGS[@]}" 2>/dev/null &
else open -a Safari "$URL"
fi

wait $SERVER_PID
