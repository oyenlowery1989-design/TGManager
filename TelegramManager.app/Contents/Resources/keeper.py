"""Session Keeper: periodically opens accounts that haven't been seen in a
while, so Telegram doesn't expire their sessions from inactivity.

Depends on state.py (config/metadata/locks) and server.py (scan_accounts,
open_account, kill_account — accessed only as `server.<attr>` inside function
bodies; see server.py's `sys.modules.setdefault` alias for why the circular
`import server` here is safe).
"""

import threading
import time
from datetime import datetime

import state
import server

_keeper_status = {
    "running":    False,
    "last_run":   None,
    "last_account": None,
    "next_check": None,
}

# Ensures the scheduled loop and the manual "Run Now" trigger never run a keeper
# pass concurrently. Acquired non-blocking; a busy caller skips its pass.
_keeper_lock = threading.Lock()

def run_keeper_loop():
    """
    Background thread: every hour, check if any account needs a keepalive open.
    Opens each due account for keeper_open_seconds, then kills just that process.
    """
    CHECK_INTERVAL = 3600   # check every hour

    while True:
        now_ts = time.time()
        _keeper_status["next_check"] = datetime.fromtimestamp(now_ts + CHECK_INTERVAL).isoformat()

        if state.config.get("keeper_enabled", False):
            if _keeper_lock.acquire(blocking=False):
                try:
                    interval_days = state.config.get("keeper_interval_days", 30)
                    open_secs     = state.config.get("keeper_open_seconds", 120)

                    _keeper_status["running"]  = True
                    _keeper_status["last_run"] = datetime.now().isoformat()

                    _run_keeper_pass(interval_days, open_secs)
                finally:
                    _keeper_status["running"] = False
                    _keeper_lock.release()
            else:
                state._log.info("run_keeper_loop: keeper already running — skipping this cycle")

        # Sleep at the END so the first check runs immediately on startup.
        time.sleep(CHECK_INTERVAL)


def _run_keeper_pass(interval_days, open_secs):
    """Open every account that hasn't been seen in interval_days. Shared by the
    scheduled loop and the manual 'Run Now' trigger."""
    for acc in server.scan_accounts():
        if acc["status"] != "ready":
            continue
        if acc["running"]:
            # Already open — counts as a keepalive; update last_opened
            with state._meta_lock:
                state.metadata.setdefault("last_opened", {})[acc["path"]] = datetime.now().isoformat()
                state.save_metadata(state.metadata)
            continue

        with state._meta_lock:
            last_iso = state.metadata.get("last_opened", {}).get(acc["path"])
        days_since = 999
        if last_iso:
            try:
                days_since = (time.time() - datetime.fromisoformat(last_iso).timestamp()) / 86400
            except Exception:
                pass

        if days_since >= interval_days:
            _keeper_status["last_account"] = acc["name"]
            ok, msg = server.open_account(acc["path"])
            if not ok:
                state._log.error("Keeper: failed to open %s: %s", acc["name"], msg)
                continue
            try:
                time.sleep(open_secs)
            finally:
                # Never leave a keeper-opened Telegram running, even if this
                # thread is interrupted mid-sleep.
                server.kill_account(acc["path"])
            time.sleep(5)   # brief pause between accounts


def trigger_keeper_now():
    """Force an immediate keeper run in a background thread.

    Returns (started, message). If a keeper pass is already in progress the
    request is rejected rather than running a second concurrent pass.
    """
    if not _keeper_lock.acquire(blocking=False):
        state._log.info("trigger_keeper_now: keeper already running — request ignored")
        return False, "keeper already running"

    def run_once():
        try:
            _keeper_status["running"]  = True
            _keeper_status["last_run"] = datetime.now().isoformat()
            _run_keeper_pass(
                state.config.get("keeper_interval_days", 30),
                state.config.get("keeper_open_seconds", 120),
            )
        finally:
            _keeper_status["running"] = False
            _keeper_lock.release()

    threading.Thread(target=run_once, daemon=True).start()
    return True, "Keeper started"
