#!/bin/bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
PORT=8477
URL="http://127.0.0.1:$PORT"
PROFILE="/tmp/TelegramManagerApp"

# Kill any previous instance on this port
lsof -ti:$PORT 2>/dev/null | xargs kill -9 2>/dev/null
sleep 0.3

# Start the server in background
python3 "$DIR/server.py" &
SERVER_PID=$!

# Wait for server to be ready (max 6 seconds)
for i in $(seq 1 30); do
    curl -s "$URL" > /dev/null 2>&1 && break
    sleep 0.2
done

# Launch in app mode - no address bar, no tabs, no extra windows
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BRAVE="/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

if [ -f "$CHROME" ]; then
    "$CHROME" \
        --app="$URL" \
        --window-size=1100,750 \
        --user-data-dir="$PROFILE" \
        --no-first-run \
        --no-default-browser-check \
        --disable-extensions \
        --disable-sync \
        2>/dev/null &
elif [ -f "$BRAVE" ]; then
    "$BRAVE" \
        --app="$URL" \
        --window-size=1100,750 \
        --user-data-dir="$PROFILE" \
        --no-first-run \
        --no-default-browser-check \
        2>/dev/null &
elif [ -f "$EDGE" ]; then
    "$EDGE" \
        --app="$URL" \
        --window-size=1100,750 \
        --user-data-dir="$PROFILE" \
        --no-first-run \
        2>/dev/null &
else
    open -a Safari "$URL"
fi

# Keep server alive
wait $SERVER_PID
