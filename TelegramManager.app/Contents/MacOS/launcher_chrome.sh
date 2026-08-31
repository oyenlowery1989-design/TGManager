#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
PORT=8477
SESSION_TOKEN="${TG_SESSION_TOKEN:-}"
if [ -z "$SESSION_TOKEN" ]; then
    SESSION_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null)
fi
if [ -z "$SESSION_TOKEN" ]; then
    SESSION_TOKEN=$(uuidgen 2>/dev/null | tr -d '-')
fi
export TG_SESSION_TOKEN="$SESSION_TOKEN"

URL="http://127.0.0.1:$PORT/$SESSION_TOKEN/"
PROFILE="$(mktemp -d "${TMPDIR:-/tmp}/TelegramManagerApp.XXXXXX")" || exit 1

python3 "$DIR/server.py" &
SERVER_PID=$!

cleanup() {
    kill "$SERVER_PID" 2>/dev/null || true
    rm -rf "$PROFILE"
}
trap cleanup EXIT

for i in $(seq 1 30); do
    curl -fsS "$URL" > /dev/null 2>&1 && break
    sleep 0.2
done

if ! curl -fsS "$URL" > /dev/null 2>&1; then
    osascript -e 'display alert "Telegram Manager" message "The local server did not start."' 2>/dev/null || true
    exit 1
fi

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

if [ -f "$CHROME" ]; then
    "$CHROME" --app="$URL" --window-size=1100,750 --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check --disable-extensions --disable-sync 2>/dev/null
elif [ -f "$BRAVE" ]; then
    "$BRAVE" --app="$URL" --window-size=1100,750 --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check 2>/dev/null
elif [ -f "$EDGE" ]; then
    "$EDGE" --app="$URL" --window-size=1100,750 --user-data-dir="$PROFILE" \
        --no-first-run --no-default-browser-check 2>/dev/null
else
    osascript -e 'display alert "Telegram Manager" message "Chrome, Brave, or Edge is required for browser fallback."' 2>/dev/null || true
    exit 1
fi
