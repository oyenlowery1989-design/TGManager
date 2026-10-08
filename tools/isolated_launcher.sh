#!/bin/bash
set -eu
umask 077
MACOS_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCES="$(cd "$MACOS_DIR/../Resources" && pwd)"
STORAGE="$HOME/Library/Application Support/TelegramManagerAyu"
for DIRECTORY in "$STORAGE" "$STORAGE/data" "$STORAGE/TG"; do
    if [ -L "$DIRECTORY" ]; then
        osascript -e 'display alert "Telegram Manager Ayu" message "Its storage folder must not be a symbolic link to another installation."' || true
        exit 1
    fi
done
mkdir -p "$STORAGE/data" "$STORAGE/TG"
chmod 700 "$STORAGE" "$STORAGE/data" "$STORAGE/TG"
SWIFT_BIN="$MACOS_DIR/launcher_swift"
SWIFT_SRC="$RESOURCES/launcher.swift"
# Never execute a stale native launcher from the original edition.
if command -v swiftc >/dev/null 2>&1; then
    if [ ! -f "$SWIFT_BIN" ] || [ "$SWIFT_SRC" -nt "$SWIFT_BIN" ]; then
        if swiftc "$SWIFT_SRC" -o "${SWIFT_BIN}.tmp" -framework Cocoa -framework WebKit -framework Foundation -O 2>"$STORAGE/data/launcher_compile.log"; then
            mv "${SWIFT_BIN}.tmp" "$SWIFT_BIN"
            chmod +x "$SWIFT_BIN"
        else
            rm -f "${SWIFT_BIN}.tmp"
        fi
    fi
    if [ -f "$SWIFT_BIN" ] && [ ! "$SWIFT_SRC" -nt "$SWIFT_BIN" ]; then
        exec "$SWIFT_BIN"
    fi
fi
# Browser fallback reads the actual port from this server's readiness record.
PYTHON="$(command -v python3)" || exit 1
TG_SESSION_TOKEN="$($PYTHON -c 'import secrets; print(secrets.token_hex(16))')"
RUNTIME="$(mktemp -d "${TMPDIR:-/tmp}/TelegramManagerAyu.XXXXXX")"
TG_READY_FILE="$RUNTIME/ready.json"
export TG_SESSION_TOKEN TG_READY_FILE
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
    rm -rf "$RUNTIME"
}
trap cleanup EXIT
"$PYTHON" "$RESOURCES/server.py" >>"$STORAGE/data/launcher.log" 2>&1 &
SERVER_PID=$!
URL=""
for i in $(seq 1 60); do
    if [ -f "$TG_READY_FILE" ]; then
        URL="$($PYTHON - "$TG_READY_FILE" <<'PY'
import json, os, sys
r=json.load(open(sys.argv[1]))
assert r['session_token'] == os.environ['TG_SESSION_TOKEN']
assert isinstance(r['port'],int) and 1024 <= r['port'] <= 65535
print('http://127.0.0.1:%d/%s/' % (r['port'],r['session_token']))
PY
)"
        if curl -fsS "$URL" >/dev/null 2>&1; then break; fi
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.2
done
if [ -z "$URL" ] || ! curl -fsS "$URL" >/dev/null 2>&1; then
    osascript -e 'display alert "Telegram Manager Ayu" message "The local server did not start. Check Library/Application Support/TelegramManagerAyu/data/launcher.log."' || true
    exit 1
fi
for BROWSER in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
    if [ -f "$BROWSER" ]; then
        "$BROWSER" --app="$URL" --window-size=1100,750 --user-data-dir="$RUNTIME/browser" --no-first-run --no-default-browser-check --disable-extensions --disable-sync
        exit 0
    fi
done
osascript -e 'display alert "Telegram Manager Ayu" message "Install Xcode command line tools for the native window, or Chrome, Brave or Edge for the browser window."' || true
exit 1
