#!/bin/bash
# Called only by the root-owned systemd service, never with caller arguments.
set -euo pipefail
umask 077
profile=/var/lib/ayugram-remote/profile
staging=/var/lib/ayugram-remote/backup-staging
unit=ayugram-remote-client.service
frozen=0
locked=0
thaw() {
    if (( frozen )); then
        /usr/bin/systemctl thaw "$unit" || return 1
        frozen=0
    fi
}
cleanup() {
    result=$?
    trap - EXIT
    thaw || result=1
    if (( locked )); then /usr/bin/rm -rf -- "$staging/profile"; fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
exec 8>/run/ayugram-remote-backup.lock
/usr/bin/flock -n 8 || exit 1
exec 9</var/lib/ayugram-remote/launch.lock
/usr/bin/flock -n 9 || exit 1
locked=1
/usr/bin/python3 -I /opt/ayugram-remote/runtime.py verify-backup >/dev/null
[ -n "${RESTIC_REPOSITORY:-}" ] && [[ "$RESTIC_REPOSITORY" == gs:* ]] || exit 1
[ "${RESTIC_PASSWORD_FILE:-}" = /etc/ayugram-remote/restic-password ] || exit 1
[ -f "$RESTIC_PASSWORD_FILE" ] && [ ! -L "$RESTIC_PASSWORD_FILE" ] || exit 1
[ "$(/usr/bin/stat -c %a:%u "$RESTIC_PASSWORD_FILE")" = 600:0 ] || exit 1
[ ! -L "$staging" ] && [ -d "$staging" ] || exit 1
/usr/bin/rm -rf -- "$staging/profile"
# The cgroup freezer waits for the whole client tree, unlike asynchronous SIGSTOP.
# Arm cleanup before freezing: failure or timeout during freeze must also thaw.
state=$(/usr/bin/systemctl show --property=ActiveState --value "$unit")
case "$state" in
    active|activating|reloading)
        frozen=1
        /usr/bin/timeout 10 /usr/bin/systemctl freeze "$unit"
        ;;
    inactive|failed) ;; # Concurrent starts wait on launch.lock until the copy is done.
    *) exit 1 ;;
esac
# All files, including SQLite databases, -wal and -shm, are copied together.
# Exclude only known disposable cache paths, never generic names/history databases.
/usr/bin/timeout --kill-after=5 45 /usr/bin/rsync -a --no-owner --no-group \
    --exclude='/tdata/emoji/' --exclude='/tdata/dumps/' \
    --exclude='/tdata/user_data/cache/' --exclude='/tdata/user_data/media_cache/' \
    "$profile/" "$staging/profile/"
thaw
exec 9<&-
cd "$staging"
# Cache disabled to fit the small VPS. No automatic forget/prune: last good backups stay.
/usr/bin/restic --no-cache backup --quiet --tag ayugram-remote --host ayugram-remote profile
