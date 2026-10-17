#!/bin/sh
set -eu
export DISPLAY=:95 XAUTHORITY=/var/lib/ayugram-remote/Xauthority
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    /usr/bin/xdpyinfo >/dev/null 2>&1 && exit 0
    sleep 1
done
exit 1
