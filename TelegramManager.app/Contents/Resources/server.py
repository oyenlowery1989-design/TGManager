#!/usr/bin/env python3
"""Telegram Manager - Backend Server v2"""

import base64
import copy
import hashlib
import hmac
import json
import os
import plistlib
import secrets
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# server.py runs as a script (python3 server.py), so it executes as module
# __main__, not "server". Alias "server" to this running module BEFORE
# importing any sibling module, so that backups.py/keeper.py's module-level
# `import server` binds this live singleton instead of re-executing this file
# a second time as a separate module (which would duplicate the log handler,
# generate a different SESSION_TOKEN, and create a parallel set of caches and
# locks). Under unittest (which imports "server" normally) this is a no-op.
import sys
sys.modules.setdefault("server", sys.modules[__name__])

import state
from state import (
    ROOT_DIR, DATA_DIR, PATH_WARNINGS,
    METADATA_FILE, WORKSPACES_FILE,
    _log, _log_file, DEFAULT_CONFIG, _sq, _as_str,
    is_safe_path,
    load_config, save_config, load_metadata, save_metadata,
    _meta_lock, _config_lock, _ws_lock, serialize_account_op, _BUSY_MSG,
    config, metadata, load_workspaces, save_workspaces,
    get_folder_size, human_size,
    _find_cache_dirs,
)
from proxy import apply_proxy, _recover_stale_proxy
from backups import (list_backups, _last_backup_map, _resolve_backup_dir,
                     delete_backup, restore_backup, backup_account)
from keeper import _keeper_status, run_keeper_loop, trigger_keeper_now

def choose_app_dialog(prompt_text, default_dir="/Applications", timeout=600):
    """Show a native macOS file picker restricted to .app bundles.

    Returns (posix_path, "") on selection, (None, "canceled") if the user
    cancels, (None, error_message) on any other failure. The AppleScript
    source contains only values escaped through _as_str() — never raw input.
    """
    lines = ["tell me to activate"]
    # Filter by UTI plus the plain "app" extension: bundles in unindexed
    # locations (DMG mounts, freshly copied folders) often have no Spotlight
    # UTI yet and would show up greyed-out with a UTI-only filter. Anything
    # picked is still validated by is_allowed_app_source() server-side.
    choose = (
        'set f to choose file of type {"com.apple.application-bundle", "com.apple.bundle", "app"}'
        " with prompt " + _as_str(prompt_text)
    )
    if os.path.isdir(default_dir):
        choose += " default location (POSIX file " + _as_str(default_dir) + ")"
    lines.append(choose)
    lines.append("POSIX path of f")
    args = ["osascript"]
    for line in lines:
        args += ["-e", line]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "picker timed out"
    if r.returncode == 0:
        return r.stdout.strip().rstrip("/"), ""
    if "-128" in (r.stderr or ""):
        return None, "canceled"
    _log.warning("choose_app_dialog failed: %s", (r.stderr or "").strip()[:200])
    return None, "picker failed: " + (r.stderr or "").strip()[:200]


# Bundles the user physically selected via the native picker this session.
# A path in here is trusted as an app-source even outside the fixed locations
# below, because summoning+clicking the native dialog needs real user presence
# — a token-only caller cannot do it.
_approved_app_sources = set()
_approved_sources_lock = threading.Lock()


def is_allowed_app_source(path):
    """Whether `path` may be used as a Telegram.app master (copied, quarantine-
    stripped, then launched).

    A token-only caller must NOT be able to point the app at an attacker-planted
    bundle. Accept only a real .app bundle that is EITHER (a) one the user just
    chose in the native picker, OR (b) inside a trusted fixed location.
    """
    if not path:
        return False
    real = os.path.realpath(path)
    if (not os.path.isdir(real) or not real.endswith(".app")
            or not os.path.isdir(os.path.join(real, "Contents", "MacOS"))):
        return False
    with _approved_sources_lock:
        if real in _approved_app_sources:
            return True
    for base in ("/Applications", ROOT_DIR, DATA_DIR):
        broot = os.path.realpath(base)
        if real == broot or real.startswith(broot + os.sep):
            return True
    return False


# ── Password-lock server-side enforcement ───────────────────────────────────
# The client no longer holds any authority over the lock: it only ever sends
# a plaintext password over the 127.0.0.1 loopback (same trust level as the
# session token itself); the server hashes/compares/tracks unlock state.

def _lock_enabled() -> bool:
    """Whether a lock password is configured. Plain dict read to match the
    rest of the file's convention of reading `config` without _config_lock."""
    return bool(config.get("lock_password_hash"))

def _verify_lock_password(password) -> bool:
    stored = config.get("lock_password_hash") or ""
    salt   = config.get("lock_password_salt") or ""
    if not stored or not isinstance(password, str):
        return False
    digest = hashlib.sha256((salt + password).encode()).hexdigest()
    return hmac.compare_digest(digest, stored)

def _server_unlock():
    global _lock_unlocked_at, _lock_last_activity, _unlock_fail_count
    with _lock_state_lock:
        _lock_unlocked_at = _lock_last_activity = time.monotonic()
        _unlock_fail_count = 0

def _server_lock():
    global _lock_unlocked_at
    with _lock_state_lock:
        _lock_unlocked_at = 0.0

def _register_unlock_failure() -> int:
    """Record a failed unlock/lock-config attempt; return the new consecutive
    failure count so the caller can throttle the response."""
    global _unlock_fail_count
    with _lock_state_lock:
        _unlock_fail_count += 1
        return _unlock_fail_count

def _check_and_touch_unlocked() -> bool:
    """Gate for lock-protected endpoints. Atomically checks whether the
    session is unlocked (and hasn't idle-timed-out) and, if so, refreshes the
    activity timestamp so the idle clock resets on real API traffic."""
    if not _lock_enabled():
        return True
    global _lock_unlocked_at, _lock_last_activity
    with _lock_state_lock:
        if _lock_unlocked_at == 0.0:
            return False
        timeout_s = max(0, int(config.get("lock_timeout_minutes") or 0)) * 60
        now = time.monotonic()
        if timeout_s and (now - _lock_last_activity) > timeout_s:
            _lock_unlocked_at = 0.0   # lazy re-lock on next request
            return False
        _lock_last_activity = now
        return True

def _is_unlocked_no_touch() -> bool:
    """Same check as _check_and_touch_unlocked() but never mutates state —
    used by /api/lock-status so status polling can't itself keep a session
    alive or mask an idle expiry."""
    if not _lock_enabled():
        return True
    with _lock_state_lock:
        if _lock_unlocked_at == 0.0:
            return False
        timeout_s = max(0, int(config.get("lock_timeout_minutes") or 0)) * 60
        if timeout_s and (time.monotonic() - _lock_last_activity) > timeout_s:
            return False
        return True

# Password-lock session state (see helpers below _server_unlock/_server_lock/
# _check_and_touch_unlocked). Guards the three fields below.
_lock_state_lock    = threading.Lock()
_lock_unlocked_at   = 0.0   # time.monotonic() of last successful unlock; 0.0 = locked
_lock_last_activity = 0.0   # time.monotonic() of last gated request that passed
_unlock_fail_count  = 0     # consecutive failed unlock attempts (throttle)

SESSION_TOKEN = os.environ.get("TG_SESSION_TOKEN", "")
_TOKEN_GENERATED = False
if not SESSION_TOKEN:
    # Never serve without a token: an empty TG_SESSION_TOKEN (launcher's two
    # generators both failing, or a manual run) would leave every endpoint
    # unauthenticated on 127.0.0.1. Generate one and print the URL to stdout
    # (not the log — the log deliberately redacts the token).
    SESSION_TOKEN = secrets.token_hex(16)
    _TOKEN_GENERATED = True
ROUTE_PREFIX  = f"/{SESSION_TOKEN}"

def _route_path(raw_path: str):
    path = urlparse(raw_path).path
    if path == ROUTE_PREFIX or path == ROUTE_PREFIX + "/":
        return "/"
    if path.startswith(ROUTE_PREFIX + "/"):
        return path[len(ROUTE_PREFIX):]
    return None

# Endpoints reachable even while the app is locked — status polling and the
# unlock/lock actions themselves. Everything else under /api/* is gated.
_LOCK_EXEMPT = {"/api/lock-status", "/api/unlock", "/api/lock"}

SKIP_NAMES = {
    "TelegramForcePortable", "Backups", ".DS_Store",
    "TelegramManager.app", "Telegram.app", "tupdates", "tdata", "modules", "_apps"
}

# The UI's <input min max> bounds (index.html) are decorative only —
# saveSettings() reads .value via bare parseInt() with no validity check, so
# an out-of-range value (e.g. negative) reaches _post_api_config unclamped
# and would otherwise make time.sleep() raise ValueError in run_keeper_loop.
_INT_KEY_BOUNDS = {
    "keeper_interval_days": (1, 365),
    "keeper_open_seconds":  (30, 600),
}

# ── Shared App (single master, APFS-cloned per account on open) ────────────
SHARED_APPS_DIR  = os.path.join(DATA_DIR, "_apps")
SHARED_MACOS_APP = os.path.join(SHARED_APPS_DIR, "macOS", "Telegram.app")

# Sibling auto-detect: Telegram.app placed next to TelegramManager.app in DATA_DIR
SIBLING_APP      = os.path.join(DATA_DIR, "Telegram.app")

def get_shared_app():
    """Return path to the shared Telegram.app master, or None.

    Priority:
      1. _apps/macOS/Telegram.app  — canonical location (set up via Setup button)
      2. ROOT_DIR/Telegram.app     — zero-config sibling placement
    """
    if os.path.isdir(SHARED_MACOS_APP):
        return SHARED_MACOS_APP
    if os.path.isdir(SIBLING_APP):
        return SIBLING_APP
    return None

def _bundle_version(app_path):
    """Return CFBundleShortVersionString of an .app bundle, or None."""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None


def _bundle_identifier(app_path):
    """Return CFBundleIdentifier of an .app bundle, or None."""
    try:
        with open(os.path.join(app_path, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except Exception:
        return None


# Only Telegram Desktop (tdesktop) reads TelegramForcePortable/tdata. The
# native macOS Telegram (ru.keepcoder.Telegram) silently ignores it and opens
# the user's personal session instead — a confusing, hard-to-diagnose mistake.
TDESKTOP_BUNDLE_ID = "com.tdesktop.Telegram"

def _wrong_app_type_error(app_path):
    """Return an error string if app_path is not Telegram Desktop, else None."""
    bid = _bundle_identifier(app_path)
    if bid == TDESKTOP_BUNDLE_ID:
        return None
    if bid == "ru.keepcoder.Telegram":
        return ("This is the native macOS Telegram — it ignores portable account "
                "data (tdata) and would open your personal session. Choose "
                "Telegram Desktop from desktop.telegram.org instead.")
    return (f"This app (bundle id {bid or 'unknown'}) is not Telegram Desktop — "
            "accounts need Telegram Desktop (com.tdesktop.Telegram) from "
            "desktop.telegram.org.")


def _safe_bundle_name(name, fallback):
    """Sanitize a display name into a safe '<name>.app' bundle folder name —
    never '', '.', '..', and never containing '/'. Falls back to `fallback`
    (itself re-sanitized) if the sanitized name would be unsafe."""
    safe = str(name or "").replace("/", "-").strip()
    if safe in ("", ".", ".."):
        safe = str(fallback or "").replace("/", "-").strip() or "Telegram"
    return safe


def _copy_app_bundle(src, dest, timeout=1800):
    """Copy a Telegram.app bundle from src to dest.

    Tries an APFS clone first (cp -cR — copy-on-write, saves ~300 MB per
    account when src/dest share a volume) and falls back to a full copy
    (cp -R) if the clone fails (e.g. cross-volume, non-APFS destination).
    Returns (ok, error_message).
    """
    r = subprocess.run(["cp", "-cR", src, dest], capture_output=True, timeout=timeout)
    if r.returncode == 0:
        return True, ""
    r = subprocess.run(["cp", "-R", src, dest], capture_output=True, timeout=timeout)
    if r.returncode == 0:
        return True, ""
    return False, r.stderr.decode(errors="replace").strip()


def clone_app_to_folder(account_path, shared_app_path=None, app_name=None):
    """Clone the shared Telegram.app into account_path.

    app_name: the display name to use for both the bundle folder name and Info.plist.
              Defaults to the account folder name.  The resulting bundle is
              '<app_name>.app' so each account has a uniquely named binary in the Dock.
    """
    if shared_app_path is None:
        shared_app_path = get_shared_app()
    if not shared_app_path or not os.path.isdir(shared_app_path):
        return False
    # If an app already exists (any name), nothing to clone
    if find_account_app(account_path):
        return True
    if app_name is None:
        with _meta_lock:
            app_name = metadata.get("dock_names", {}).get(account_path) or os.path.basename(account_path)
    safe_bundle_name = _safe_bundle_name(app_name, os.path.basename(account_path))
    app_dest = os.path.join(account_path, safe_bundle_name + ".app")
    ok, _err = _copy_app_bundle(shared_app_path, app_dest)
    if not ok:
        return False
    # Strip Gatekeeper quarantine so macOS doesn't silently block launch
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", app_dest], capture_output=True, timeout=120)
    # NOTE: We deliberately do NOT patch Info.plist or re-sign here. Modifying the
    # bundle in the per-open clone path triggers an APFS copy-on-write split and an
    # ad-hoc re-sign that changes the app's code identity — which has corrupted tdata
    # sessions in the past. The Dock display name is applied only via the explicit
    # dock-name action (set_telegram_display_name), never on every open.
    return True

def find_account_app(account_path: str):
    """Return the path to the Telegram app bundle inside account_path, or None.

    Checks for the original 'Telegram.app' name first (fast path for existing accounts),
    then scans for any *.app bundle that contains a Contents/MacOS/Telegram binary
    (handles bundles renamed to the account name).
    """
    std = os.path.join(account_path, "Telegram.app")
    if os.path.isdir(std):
        return std
    try:
        for entry in sorted(os.listdir(account_path)):
            if not entry.endswith(".app"):
                continue
            candidate = os.path.join(account_path, entry)
            if os.path.isfile(os.path.join(candidate, "Contents", "MacOS", "Telegram")):
                return candidate
    except OSError:
        pass
    return None


def _find_fallback_app_source(accs):
    """Newest-mtime per-account Telegram.app copy among accs, or None.

    Shared by /api/shared-app/status's "what setup will use" preview and
    /api/shared-app/setup's actual fallback selection — they used to pick
    differently (status: first account in scan order; setup: newest mtime),
    so the preview could name a different account than setup actually used.
    """
    latest_mod, latest = 0, None
    for acc in accs:
        ap = find_account_app(acc["path"])
        if ap:
            mod = os.path.getmtime(ap)
            if mod > latest_mod:
                latest_mod, latest = mod, ap
    return latest


def remove_cloned_app(account_path):
    if not get_shared_app():
        return
    app = find_account_app(account_path)
    if app and os.path.isdir(app):
        subprocess.run(["rm", "-rf", app], capture_output=True, timeout=300)


def clear_account_caches(account_path, threshold_mb=0):
    """Delete all regenerable caches inside the account's tdata (media, file,
    emoji and the bot-WebView Chromium caches) — never session/login data.
    Refuses while Telegram is running. threshold_mb>0 clears only when the
    total exceeds it; 0 = always. Returns (cleared, freed_bytes)."""
    tdata = os.path.join(account_path, "TelegramForcePortable", "tdata")
    if not os.path.isdir(tdata):
        return False, 0
    if find_telegram_pid(account_path):
        _log.info("clear_account_caches: skipped — Telegram running for %s", account_path)
        return False, 0
    targets = _find_cache_dirs(tdata)
    total = sum(get_folder_size(t) for t in targets)
    if threshold_mb > 0 and total < threshold_mb * 1024 * 1024:
        return False, 0
    for t in targets:
        subprocess.run(["rm", "-rf", t], capture_output=True, timeout=120)
    invalidate_tdata_size(account_path)
    _log.info("Cleared %d cache dir(s) for %s (freed %s)",
              len(targets), os.path.basename(account_path), human_size(total))
    return True, total


def scan_accounts():
    """Recursively find all account folders across ROOT_DIR and extra_scan_dirs."""
    accounts = []
    seen_paths = set()

    # Snapshot metadata once under lock so the scan sees a consistent view
    with _meta_lock:
        _meta = copy.deepcopy(metadata)

    # Use the shared ps cache — all accounts share one ps call per second
    _ps_out = _get_ps_output()

    def _is_running(folder_path):
        prefix = folder_path.rstrip("/") + "/"
        return any(
            prefix in line and ".app/Contents/MacOS/Telegram" in line
            for line in _ps_out.split("\n")
        )

    _shared_app = get_shared_app()

    def scan_dir(path, depth=0, group_parts=None):
        if depth > 5:
            return
        if group_parts is None:
            group_parts = []
        try:
            entries = sorted(os.listdir(path))
        except (PermissionError, FileNotFoundError):
            return

        for name in entries:
            if name.startswith(".") or name in SKIP_NAMES or name.endswith(".lnk"):
                continue
            full_path = os.path.join(path, name)
            if not os.path.isdir(full_path):
                continue
            if full_path in seen_paths:
                continue

            tdata_path    = os.path.join(full_path, "TelegramForcePortable", "tdata")
            raw_tdata     = os.path.join(full_path, "tdata")
            has_app       = find_account_app(full_path) is not None
            has_tdata     = os.path.isdir(tdata_path)
            has_raw_tdata = os.path.isdir(raw_tdata)
            has_portable  = os.path.isdir(os.path.join(full_path, "TelegramForcePortable"))

            can_open = has_app or bool(_shared_app)
            if has_tdata or has_raw_tdata or (can_open and has_portable):
                seen_paths.add(full_path)
                rel_parts = group_parts + [name]
                rel_path  = " / ".join(rel_parts)
                group     = " / ".join(group_parts) if group_parts else "Root"

                if can_open and has_tdata:
                    status = "ready"
                elif has_raw_tdata and not has_tdata:
                    status = "needs_setup"   # tdata in wrong place
                elif can_open and not has_tdata:
                    status = "no_data"
                elif not can_open and has_tdata:
                    status = "needs_setup"   # valid tdata, just needs app
                else:
                    status = "broken"        # no tdata at all

                tdata_actual = tdata_path if has_tdata else (raw_tdata if has_raw_tdata else None)
                tdata_size   = cached_tdata_size(tdata_actual) if tdata_actual else 0
                health       = check_health(full_path, has_app, has_tdata, tdata_actual)

                accounts.append({
                    "name":             name,
                    "path":             full_path,
                    "rel_path":         rel_path,
                    "group":            group,
                    "status":           status,
                    "running":          _is_running(full_path),
                    "has_app":          has_app,
                    "has_tdata":        has_tdata or has_raw_tdata,
                    "can_backup":       has_tdata,
                    "tdata_size":       tdata_size,
                    "tdata_size_human": human_size(tdata_size),
                    "note":             _meta.get("notes",        {}).get(full_path, ""),
                    "username":         _meta.get("usernames",    {}).get(full_path, ""),
                    "order":            _meta.get("order",        {}).get(full_path, 9999),
                    "color":            _meta.get("colors",       {}).get(full_path, ""),
                    "last_opened":      _meta.get("last_opened",  {}).get(full_path, ""),
                    "pinned":           full_path in _meta.get("pinned", []),
                    "proxy":            _meta.get("proxies",      {}).get(full_path),
"uses_shared_app":  not has_app and bool(_shared_app),
                    "dock_name":        _meta.get("dock_names",    {}).get(full_path, ""),
                    "avatar":           _meta.get("avatars",       {}).get(full_path, ""),
                    "health":           health,
                })
            else:
                scan_dir(full_path, depth + 1, group_parts + [name])

    # Always scan the primary root
    scan_dir(ROOT_DIR)

    # Scan any extra folders the user configured
    for extra in config.get("extra_scan_dirs", []):
        extra = os.path.expanduser(extra)
        if os.path.isdir(extra):
            # Use the folder name as the top-level group prefix
            folder_name = os.path.basename(extra.rstrip("/"))
            scan_dir(extra, depth=0, group_parts=[f"📂 {folder_name}"])

    accounts.sort(key=lambda a: (0 if a["pinned"] else 1, a["group"], a["order"], a["name"]))
    return accounts

# Per-account tdata size cache. get_folder_size() is a full tree walk; the
# account scan ran it for every account on every scan, and the UI polls faster
# than the scan-cache TTL, so a big install re-walked every tdata continuously.
# A tdata's size only changes when the account is opened, closed, or its cache
# is cleared — so cache it and invalidate on exactly those events.
_tdata_size_cache = {}          # realpath(tdata) -> (size_bytes, ts)
_tdata_size_lock  = threading.Lock()
_TDATA_SIZE_TTL   = 60

def cached_tdata_size(tdata_path):
    key = os.path.realpath(tdata_path)
    now = time.time()
    with _tdata_size_lock:
        ent = _tdata_size_cache.get(key)
        if ent and now - ent[1] < _TDATA_SIZE_TTL:
            return ent[0]
    size = get_folder_size(tdata_path)   # walk outside the lock
    with _tdata_size_lock:
        _tdata_size_cache[key] = (size, now)
    return size

def invalidate_tdata_size(account_path):
    """Drop the cached size for an account's tdata after it changed."""
    tdata = os.path.join(account_path, "TelegramForcePortable", "tdata")
    with _tdata_size_lock:
        _tdata_size_cache.pop(os.path.realpath(tdata), None)

# ── Disk-size cache (slow walk, refreshed in background every 60 s) ─────────
# Kept separate from the 4-second scan cache so expensive ROOT_DIR walks
# never block the hot /api/accounts path.
_disk_stats      = {"data": None, "ts": 0.0}
_disk_stats_lock = threading.Lock()
_disk_stats_bg   = threading.Lock()   # prevents duplicate background refreshes
_DISK_STATS_TTL  = 60                 # seconds

def _compute_disk_stats():
    """Walk ROOT_DIR + Backups to get total sizes. May take several seconds."""
    tg_size     = get_folder_size(ROOT_DIR)
    backup_root = os.path.join(DATA_DIR, "Backups")
    backup_size = get_folder_size(backup_root) if os.path.isdir(backup_root) else 0
    accs        = scan_accounts_cached()
    # Full reclaimable cache (media + file + emoji + WebView), not just
    # media_cache — this is the number the "Clear Caches" button would free.
    # Fine to walk here: disk stats are background-refreshed on a 60s cache.
    cache_total = 0
    for a in accs:
        tdata = os.path.join(a["path"], "TelegramForcePortable", "tdata")
        if os.path.isdir(tdata):
            cache_total += sum(get_folder_size(t) for t in _find_cache_dirs(tdata))
    result = {"tg_size": tg_size, "backup_size": backup_size, "cache_total": cache_total}
    with _disk_stats_lock:
        _disk_stats["data"] = result
        _disk_stats["ts"]   = time.time()
    return result

def get_disk_stats():
    """Return disk size stats from cache. Blocks only on the very first call;
    subsequent stale refreshes happen in a background thread."""
    with _disk_stats_lock:
        age    = time.time() - _disk_stats["ts"]
        cached = _disk_stats["data"]
    if cached is not None and age < _DISK_STATS_TTL:
        return cached
    if cached is None:
        return _compute_disk_stats()   # first call — must block
    # Stale: refresh in background, return last known data immediately
    if _disk_stats_bg.acquire(blocking=False):
        def _bg():
            try:
                _compute_disk_stats()
            finally:
                _disk_stats_bg.release()
        threading.Thread(target=_bg, daemon=True).start()
    return cached

def check_health(folder_path, has_app, has_tdata, tdata_path):
    """Check account integrity and session freshness."""
    issues = []
    expiry = None   # "fresh" | "stale" | "expired"

    errors = []  # data-level problems (red)
    warns  = []  # recoverable / informational (yellow)

    if not has_app:
        if get_shared_app():
            pass  # will be cloned from shared master on open
        else:
            warns.append("Telegram.app missing — use Setup to add it")
    if not has_tdata:
        errors.append("tdata missing")
    elif tdata_path and os.path.isdir(tdata_path):
        try:
            entries = os.listdir(tdata_path)
            if len(entries) == 0:
                errors.append("tdata is empty")
            elif not any(e.startswith("map") or e == "key_datas" or len(e) > 8 for e in entries):
                warns.append("tdata may be incomplete")
            else:
                # Session freshness: Telegram expires sessions after ~180 days of inactivity
                key_files = [os.path.join(tdata_path, f)
                             for f in entries if f in ("key_datas", "map0", "map1") or
                             (len(f) > 8 and not f.endswith(".tmp"))]
                mtimes = [os.path.getmtime(f) for f in key_files if os.path.isfile(f)]
                if mtimes:
                    newest_mod = max(mtimes)
                    days_old = (time.time() - newest_mod) / 86400
                    if days_old > 180:
                        expiry = "expired"
                        warns.append(f"No activity for {int(days_old)} days — session may be expired")
                    elif days_old > 60:
                        expiry = "stale"
                        warns.append(f"No activity for {int(days_old)} days")
                    else:
                        expiry = "fresh"
        except Exception:
            errors.append("tdata unreadable")

    issues = errors + warns
    if errors:
        return {"status": "error", "issues": issues, "expiry": expiry}
    if warns:
        return {"status": "warn", "issues": issues, "expiry": expiry}
    return {"status": "ok", "issues": [], "expiry": expiry or "fresh"}


_ps_cache      = {"output": "", "ts": 0.0}
_ps_cache_lock = threading.Lock()
_PS_TTL        = 1.0  # seconds — short enough to be current, long enough to amortize

# ── App-exit watcher: clean up cloned apps when Telegram quits outside manager
_watcher_grace      = {}   # path → expiry_time; paths exempt from watcher cleanup
_watcher_grace_lock = threading.Lock()

def _watcher_exempt(path, seconds=60):
    """Grace period after open_account — skip watcher for this path until expiry."""
    with _watcher_grace_lock:
        _watcher_grace[path] = time.time() + seconds

def _app_watcher_loop():
    """Remove cloned Telegram.app bundles when the user quits Telegram outside the manager.

    Polls running processes every 5 s.  For every account folder that has a local
    app bundle but no running Telegram process, removes the bundle so it stays
    clean.  Safe because the shared master re-clones it on next open.
    """
    time.sleep(10)   # startup grace: let any already-running accounts settle
    # Per-path count of consecutive cycles observed "idle" — a bundle is only
    # removed after it has been idle for _IDLE_CYCLES_REQUIRED cycles in a row.
    idle_counts = {}
    _IDLE_CYCLES_REQUIRED = 2
    while True:
        any_running = False
        try:
            now = time.time()
            with _watcher_grace_lock:
                expired = [p for p, exp in _watcher_grace.items() if exp <= now]
                for p in expired:
                    del _watcher_grace[p]
            threshold_mb = config.get("auto_clear_cache_mb", 0)

            # If we can't read the process list, treat every account as "unknown"
            # and skip the entire deletion pass this cycle to avoid false removals.
            ps_known = bool(_get_ps_output())

            live_paths = set()
            for acc in scan_accounts_cached():
                p = acc["path"]
                live_paths.add(p)

                # Re-check grace freshly under the lock right before deciding —
                # open_account may have armed the exemption after our snapshot.
                with _watcher_grace_lock:
                    exempt = _watcher_grace.get(p, 0) > time.time()
                if exempt:
                    any_running = True
                    idle_counts[p] = 0
                    continue

                # Re-check the live process state immediately before deleting.
                if not ps_known or is_running(p):
                    any_running = True
                    idle_counts[p] = 0
                    continue

                # Observed idle this cycle — require N consecutive idle cycles.
                idle_counts[p] = idle_counts.get(p, 0) + 1
                if idle_counts[p] < _IDLE_CYCLES_REQUIRED:
                    continue

                # Final freshness re-check under the lock + live process check
                # right before removal, in case state changed mid-loop.
                with _watcher_grace_lock:
                    exempt = _watcher_grace.get(p, 0) > time.time()
                if exempt or is_running(p):
                    any_running = True
                    idle_counts[p] = 0
                    continue

                # Remove cloned app bundle (only if shared master exists)
                if get_shared_app():
                    app = find_account_app(p)
                    if app:
                        _log.info("Watcher: removing idle cloned app for %s", acc["name"])
                        subprocess.run(["rm", "-rf", app], capture_output=True, timeout=300)
                        invalidate_scan_cache()
                # Auto-clear caches (media + WebView) if total is over threshold
                if threshold_mb > 0:
                    clear_account_caches(p, threshold_mb)
                idle_counts[p] = 0

            # Drop idle counters for accounts that no longer exist
            for gone in [p for p in idle_counts if p not in live_paths]:
                del idle_counts[gone]
        except Exception as e:
            _log.warning("_app_watcher_loop: unhandled error: %s", e, exc_info=True)
            any_running = False
        # Poll frequently while accounts are open; back off when everything is idle
        time.sleep(5 if any_running else 30)

def _get_ps_output() -> str:
    """Return cached `ps` output, refreshing at most once per second.

    scan_accounts(), find_telegram_pid(), and is_running() all need the process
    list. Without this cache they each fork a full ps(1) invocation; with it they
    share a single call for any burst of activity within 1 s.
    """
    with _ps_cache_lock:
        if time.time() - _ps_cache["ts"] < _PS_TTL:
            return _ps_cache["output"]
    try:
        output = subprocess.run(
            ["ps", "-e", "-o", "pid=,args="], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception as e:
        # On failure, keep serving the last good output rather than caching "".
        # Do NOT advance the timestamp so the next call retries immediately.
        _log.warning("_get_ps_output: ps failed (%s); returning last cached output", e)
        with _ps_cache_lock:
            return _ps_cache["output"]
    if not output:
        # Empty output is treated as "unknown" — don't advance ts so we retry,
        # and fall back to the last good cached output if we have one.
        with _ps_cache_lock:
            return _ps_cache["output"]
    with _ps_cache_lock:
        _ps_cache["output"] = output
        _ps_cache["ts"]     = time.time()
    return output

def is_running(folder_path):
    """Single-account running check (used outside scan_accounts)."""
    prefix = folder_path.rstrip("/") + "/"
    return any(
        prefix in line and ".app/Contents/MacOS/Telegram" in line
        for line in _get_ps_output().split("\n")
    )

# Short-lived scan cache: /api/stats and /api/alerts called in the same browser
# refresh as /api/accounts — reuse the result if it's < 4 seconds old.
#
# Two-lock design:
#   _scan_cache_lock  — cheap, guards only the cache dict (held briefly)
#   _scan_exec_lock   — serialises actual scans so concurrent cache-misses
#                       don't all run full filesystem walks in parallel
_scan_cache_lock = threading.Lock()
_scan_exec_lock  = threading.Lock()
_scan_cache      = {"data": None, "ts": 0.0}
_SCAN_TTL        = 4  # seconds

def scan_accounts_cached():
    # Fast path — return a copy so callers can't mutate the cached list
    with _scan_cache_lock:
        if _scan_cache["data"] is not None and time.time() - _scan_cache["ts"] < _SCAN_TTL:
            return list(_scan_cache["data"])

    # Slow path — only one thread scans at a time; others wait and reuse the result
    with _scan_exec_lock:
        # Re-check: another thread may have populated the cache while we waited
        with _scan_cache_lock:
            if _scan_cache["data"] is not None and time.time() - _scan_cache["ts"] < _SCAN_TTL:
                return list(_scan_cache["data"])
        result = scan_accounts()
        with _scan_cache_lock:
            _scan_cache["data"] = result
            _scan_cache["ts"]   = time.time()
        return list(result)

def invalidate_scan_cache():
    with _scan_cache_lock:
        _scan_cache["data"] = None

@serialize_account_op(lambda path: path, (False, _BUSY_MSG))
def open_account(path):
    """Open the Telegram account at path. Returns (ok, message)."""
    _log.info("Opening account: %s", path)
    if is_running(path):
        _log.info("open_account: already running for %s", path)
        return True, "already running"
    app = find_account_app(path)
    # A leftover clone from an older master would silently launch the old
    # version forever — replace it when its version differs from the master.
    shared = get_shared_app()
    if app and shared:
        clone_id  = (_bundle_identifier(app), _bundle_version(app))
        master_id = (_bundle_identifier(shared), _bundle_version(shared))
        if all(clone_id) and all(master_id) and clone_id != master_id:
            _log.info("open_account: replacing stale clone %s with master %s for %s",
                      clone_id, master_id, path)
            subprocess.run(["rm", "-rf", app], capture_output=True, timeout=300)
            app = None
    if not app:
        with _meta_lock:
            dock_name = metadata.get("dock_names", {}).get(path) or os.path.basename(path)
        if not clone_app_to_folder(path, app_name=dock_name):
            msg = "No Telegram.app found and cloning failed — set up a shared app first"
            _log.warning("open_account: %s (%s)", msg, path)
            return False, msg
        app = find_account_app(path)
        if not app:
            msg = "Telegram.app not found after cloning — check the shared app master"
            _log.warning("open_account: %s (%s)", msg, path)
            return False, msg

    with _meta_lock:
        proxy = metadata.get("proxies", {}).get(path)
    if proxy and proxy.get("host"):
        if config.get("proxy_system_apply"):
            _log.info("Applying proxy %s:%s for account %s", proxy.get("host"), proxy.get("port"), path)
            apply_proxy(proxy)
        else:
            _log.info("Proxy stored for %s but system-wide apply is disabled — "
                      "set the proxy inside Telegram, or enable it in Settings", path)

    try:
        # -n launches a new instance of this specific bundle (not by app name).
        r = subprocess.run(["open", "-n", app], capture_output=True, timeout=10)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip() or "unknown error"
            _log.error("open_account: 'open' command failed for %s: %s", path, err)
            return False, f"Failed to launch Telegram: {err}"
    except Exception as e:
        _log.error("open_account: exception launching %s: %s", path, e)
        return False, f"Failed to launch Telegram: {e}"
    # Arm the watcher grace period AFTER launch begins so the watcher doesn't
    # remove the cloned app before the Telegram process appears in the ps list.
    _watcher_exempt(path)

    with _meta_lock:
        metadata.setdefault("last_opened", {})[path] = datetime.now().isoformat()
        save_metadata(metadata)

    invalidate_tdata_size(path)   # a running account writes tdata; drop stale size
    return True, ""


def _open_accounts_async(accounts, tag):
    """Open a filtered list of accounts one at a time in a background thread,
    staggered 0.5s apart (so macOS doesn't choke on a burst of launches).
    Shared by /api/open-all, /api/open-group, /api/open-pinned, and
    /api/workspace/open — `tag` only affects the failure log line."""
    def _run():
        for acc in accounts:
            ok, msg = open_account(acc["path"])
            if not ok:
                _log.error("%s: failed to open %s: %s", tag, acc["name"], msg)
            time.sleep(0.5)
        invalidate_scan_cache()
    threading.Thread(target=_run, args=(), daemon=True).start()


def _validate_import_string_map(section_name, value):
    if not isinstance(value, dict):
        return False, f"{section_name} must be an object"
    for key, item in value.items():
        if not isinstance(key, str):
            return False, f"{section_name} keys must be strings"
        if not isinstance(item, str):
            return False, f"{section_name} values must be strings"
    return True, ""


def _validate_import_payload(data):
    if not isinstance(data, dict):
        return False, "Invalid export file — expected a JSON object", None

    metadata_in = data.get("metadata")
    config_in = data.get("config")
    workspaces_in = data.get("workspaces", {})

    if not isinstance(metadata_in, dict) or not isinstance(config_in, dict) or not isinstance(workspaces_in, dict):
        return False, "Invalid export file — malformed metadata/config/workspaces", None

    allowed_metadata = {
        "notes": {}, "usernames": {}, "order": {}, "colors": {},
        "last_opened": {}, "pinned": [], "proxies": {}, "dock_names": {},
        "avatars": {}
    }
    cleaned_metadata = {}

    for key, default_value in allowed_metadata.items():
        if key not in metadata_in:
            cleaned_metadata[key] = copy.deepcopy(default_value)
            continue
        value = metadata_in[key]
        if key == "pinned":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return False, "metadata.pinned must be an array of strings", None
            cleaned_metadata[key] = list(value)
        elif key == "order":
            if not isinstance(value, dict):
                return False, "metadata.order must be an object", None
            cleaned = {}
            for path, order in value.items():
                if not isinstance(path, str) or not isinstance(order, int):
                    return False, "metadata.order must map string paths to integers", None
                cleaned[path] = order
            cleaned_metadata[key] = cleaned
        elif key == "proxies":
            if not isinstance(value, dict):
                return False, "metadata.proxies must be an object", None
            cleaned = {}
            for path, proxy in value.items():
                if not isinstance(path, str) or not isinstance(proxy, dict):
                    return False, "metadata.proxies must map string paths to proxy objects", None
                cleaned_proxy = {}
                for proxy_key in ("type", "host", "user", "pass"):
                    proxy_val = proxy.get(proxy_key)
                    if proxy_val is not None and not isinstance(proxy_val, str):
                        return False, "proxy fields must be strings or null", None
                    if proxy_val is not None:
                        cleaned_proxy[proxy_key] = proxy_val
                port_val = proxy.get("port")
                if port_val is not None and not isinstance(port_val, int):
                    return False, "proxy port must be an integer or null", None
                if port_val is not None:
                    cleaned_proxy["port"] = port_val
                cleaned[path] = cleaned_proxy
            cleaned_metadata[key] = cleaned
        else:
            ok, msg = _validate_import_string_map(f"metadata.{key}", value)
            if not ok:
                return False, msg, None
            cleaned_metadata[key] = dict(value)

    allowed_config_keys = {
        "app_source", "port", "extra_scan_dirs", "keeper_enabled",
        "keeper_interval_days", "keeper_open_seconds", "auto_clear_cache_mb",
        "proxy_system_apply", "backup_keep_per_account",
        "lock_password_hash", "lock_password_salt", "lock_hint", "lock_timeout_minutes"
    }
    cleaned_config = {k: copy.deepcopy(v) for k, v in DEFAULT_CONFIG.items()}
    for key in allowed_config_keys:
        if key not in config_in:
            continue
        value = config_in[key]
        if key in ("app_source", "lock_password_hash", "lock_password_salt", "lock_hint"):
            if value is not None and not isinstance(value, str):
                return False, f"config.{key} must be a string or null", None
            if key == "app_source" and value and not is_allowed_app_source(value):
                # Same trust check /api/config applies — a token-only caller
                # must not be able to point app_source at an attacker-planted
                # bundle via import (it's later copied+launched unchecked by
                # create_account/setup_account/update_all_apps/repair).
                _log.warning("import-config: dropping untrusted app_source=%r", value)
                value = ""
        elif key in ("keeper_enabled", "proxy_system_apply"):
            if not isinstance(value, bool):
                return False, f"config.{key} must be a boolean", None
        elif key in ("port", "keeper_interval_days", "keeper_open_seconds", "auto_clear_cache_mb", "backup_keep_per_account", "lock_timeout_minutes"):
            if not isinstance(value, int):
                return False, f"config.{key} must be an integer", None
        elif key == "extra_scan_dirs":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return False, "config.extra_scan_dirs must be an array of strings", None
        cleaned_config[key] = value

    if not isinstance(cleaned_config.get("port", 8477), int) or not (1024 <= cleaned_config["port"] <= 65535):
        return False, "config.port must be an integer between 1024 and 65535", None

    cleaned_workspaces = {}
    for name, workspace in workspaces_in.items():
        if not isinstance(name, str) or not isinstance(workspace, dict):
            return False, "workspaces must map names to objects", None
        accounts = workspace.get("accounts", [])
        if not isinstance(accounts, list) or not all(isinstance(item, str) for item in accounts):
            return False, f"workspace {name!r} must contain an array of account paths", None
        icon = workspace.get("icon", "📁")
        created = workspace.get("created", "")
        if not isinstance(icon, str) or not isinstance(created, str):
            return False, f"workspace {name!r} has invalid icon/created fields", None
        cleaned_workspaces[name] = {"accounts": list(accounts), "icon": icon, "created": created}

    return True, "", {"metadata": cleaned_metadata, "config": cleaned_config, "workspaces": cleaned_workspaces}


# ── Per-process Telegram control ───────────────────────────────────────────

def find_telegram_pid(account_path):
    """Return the PID of the Telegram process that launched from this account folder."""
    prefix = account_path.rstrip("/") + "/"
    for line in _get_ps_output().split("\n"):
        if prefix in line and ".app/Contents/MacOS/Telegram" in line:
            try:
                return int(line.strip().split()[0])
            except (ValueError, IndexError):
                pass
    return None

def _pid_alive(pid):
    """True if the process still exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_account(account_path):
    """Kill only the Telegram process for this account."""
    pid = find_telegram_pid(account_path)
    if pid:
        _log.info("Killing Telegram PID %d for account %s", pid, account_path)
        subprocess.run(["kill", str(pid)], capture_output=True, timeout=10)
        def _cleanup(p, pid):
            # Wait for Telegram to actually exit (it may still be flushing
            # tdata) before touching the cloned app; escalate to SIGKILL if
            # it ignores SIGTERM.
            deadline = time.time() + 8
            while _pid_alive(pid) and time.time() < deadline:
                time.sleep(0.5)
            if _pid_alive(pid):
                _log.warning("kill_account: PID %d ignored SIGTERM — sending SIGKILL", pid)
                subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=10)
                deadline = time.time() + 4
                while _pid_alive(pid) and time.time() < deadline:
                    time.sleep(0.5)
            remove_cloned_app(p)
            invalidate_tdata_size(p)   # tdata settled (and cache may auto-clear on close)
            invalidate_scan_cache()
        threading.Thread(target=_cleanup, args=(account_path, pid), daemon=True).start()
        return True
    return False




def _patch_app_display_name(app_path: str, display_name: str) -> bool:
    """Patch CFBundleDisplayName + CFBundleName in app_path's Info.plist and re-sign.

    Low-level helper: takes the .app bundle path directly.
    Returns True on success.
    """
    plist = os.path.join(app_path, "Contents", "Info.plist")
    if not os.path.exists(plist):
        return False
    pb = "/usr/libexec/PlistBuddy"
    safe_dn = shlex.quote(display_name)
    for key in ("CFBundleDisplayName", "CFBundleName"):
        r = subprocess.run([pb, "-c", f"Set :{key} {safe_dn}", plist],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            subprocess.run([pb, "-c", f"Add :{key} string {safe_dn}", plist],
                           capture_output=True, timeout=60)
    # Re-sign with ad-hoc signature (required after modifying Info.plist)
    r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        _log.warning("_patch_app_display_name: codesign failed for %s: %s",
                     app_path, r.stderr.decode(errors="replace").strip())
    return True


def set_telegram_display_name(folder_path, display_name):
    """Set the Dock / Finder name for the app bundle inside folder_path.

    Locates the bundle via find_account_app() (handles renamed bundles),
    patches Info.plist, and re-signs with an ad-hoc signature.
    """
    app_path = find_account_app(folder_path)
    if not app_path:
        return False, "No Telegram app bundle found in this account folder"
    ok = _patch_app_display_name(app_path, display_name)
    if ok:
        return True, f'Dock name set to "{display_name}"'
    return False, "Info.plist not found"


def fix_all_dock_names():
    """Apply each account's dock name (custom if set, else folder name) to its app bundle."""
    results = []
    with _meta_lock:
        dock_names_snap = metadata.get("dock_names", {}).copy()
    for acc in scan_accounts():
        name = dock_names_snap.get(acc["path"]) or acc["name"]
        ok, msg = set_telegram_display_name(acc["path"], name)
        results.append({"name": acc["name"], "ok": ok, "msg": msg})
    return results


def rename_account(old_path, new_name):
    """Rename an account folder and update all metadata keys.

    Uses a two-phase commit so a crash or I/O error at any point leaves both the
    folder and the metadata file in a consistent state:
      Phase 1 – build updated metadata in a local deep-copy (global untouched).
      Phase 2 – rename the folder (os.rename is atomic on the same filesystem).
      Phase 3 – persist the metadata copy (atomic .tmp → os.replace).
      Rollback – if Phase 3 fails, undo the folder rename before returning the error.
      Commit   – only after both writes succeed do we update the in-memory global.
    """
    _log.info("Renaming account %s → %r", old_path, new_name)
    if is_running(old_path):
        _log.warning("rename_account: refused — Telegram is running for %s", old_path)
        return False, "Close Telegram first"
    parent   = os.path.dirname(old_path)
    new_path = os.path.join(parent, new_name)
    if os.path.exists(new_path):
        _log.warning("rename_account: target already exists: %s", new_path)
        return False, f'A folder named "{new_name}" already exists'

    # Phase 1: build updated metadata without touching the global dict yet
    with _meta_lock:
        new_meta = copy.deepcopy(metadata)
    for section in ("notes", "usernames", "order", "colors",
                    "last_opened", "proxies", "dock_names", "avatars"):
        d = new_meta.get(section, {})
        if old_path in d:
            d[new_path] = d.pop(old_path)
    pinned = new_meta.get("pinned", [])
    if old_path in pinned:
        pinned[pinned.index(old_path)] = new_path

    # Phase 2: rename folder on disk
    try:
        os.rename(old_path, new_path)
    except Exception as e:
        _log.warning("rename_account: os.rename failed: %s", e)
        return False, str(e)

    # Phase 3: persist metadata (atomic .tmp → replace)
    try:
        save_metadata(new_meta)
    except Exception as e:
        # Undo the folder rename so disk and metadata stay consistent
        try:
            os.rename(new_path, old_path)
            _log.warning("rename_account: metadata save failed (%s); folder rename undone", e)
        except Exception as undo_e:
            _log.error(
                "rename_account: CRITICAL — folder is at %s but metadata save failed (%s) "
                "and undo also failed (%s). Manual fix required.", new_path, e, undo_e
            )
        return False, f"Could not save metadata: {e}"

    # Commit: update in-memory global only after both disk operations succeeded
    with _meta_lock:
        metadata.clear()
        metadata.update(new_meta)

    # Update the Dock name to match the new folder name
    set_telegram_display_name(new_path, new_name)

    _log.info("Account renamed to %s", new_path)
    return True, new_path

def list_groups():
    """
    Return a list of {name, path} dicts for every unique group currently on disk,
    plus "Root" (ROOT_DIR itself). Used to let the user pick where to create a new account.
    """
    groups = {}
    for acc in scan_accounts_cached():
        g = acc["group"]
        if g not in groups:
            # The group's parent directory is the dirname of any account inside it
            groups[g] = os.path.dirname(acc["path"])
    # Always include Root even if no accounts live there yet
    groups.setdefault("Root", ROOT_DIR)
    # Sort: Root first, then alphabetically
    ordered = [{"name": "Root", "path": ROOT_DIR}]
    for name in sorted(k for k in groups if k != "Root"):
        ordered.append({"name": name, "path": groups[name]})
    return ordered


def create_account(name, parent_path, open_after=True):
    """
    Create a brand-new account folder, copy Telegram.app into it, and open it
    so the user can log in. Returns (success, path_or_error_msg).
    """
    _log.info("Creating account %r in %s", name, parent_path or ROOT_DIR)
    # Sanitise name
    name = name.strip()
    bad_chars = set('/\\:*?"<>|')
    if not name:
        return False, "Account name cannot be empty"
    if any(c in bad_chars for c in name):
        return False, "Name contains invalid characters (/ \\ : * ? \" < > |)"
    if name in (".", "..") or name.startswith("."):
        return False, "Account name cannot start with a dot"
    if name in SKIP_NAMES:
        return False, f'"{name}" is a reserved folder name'

    # Validate parent path
    if not parent_path:
        parent_path = ROOT_DIR
    if not os.path.isdir(parent_path):
        return False, f"Parent folder does not exist: {parent_path}"

    folder_path = os.path.join(parent_path, name)
    if os.path.exists(folder_path):
        return False, f'A folder named "{name}" already exists in that location'

    # Create account folder + TelegramForcePortable marker
    try:
        os.makedirs(folder_path, exist_ok=True)
        os.makedirs(os.path.join(folder_path, "TelegramForcePortable"), exist_ok=True)
    except Exception as e:
        return False, f"Could not create folder: {e}"

    # Copy Telegram.app from source, naming the bundle after the account.
    # Priority: explicit config → shared master (incl. sibling auto-detect) → existing account copy
    app_source = config.get("app_source", "")
    if not os.path.isdir(app_source):
        app_source = get_shared_app() or ""
    if not os.path.isdir(app_source):
        # Last resort: borrow from an existing account
        for acc in scan_accounts_cached():
            candidate = find_account_app(acc["path"])
            if candidate:
                app_source = candidate
                break
    if not os.path.isdir(app_source):
        return False, (
            "Telegram.app not found. Place Telegram.app next to TelegramManager.app, "
            "or set the correct path in Settings."
        )

    safe_bundle = _safe_bundle_name(name, os.path.basename(folder_path))
    app_dest = os.path.join(folder_path, safe_bundle + ".app")
    ok, err = _copy_app_bundle(app_source, app_dest)
    if not ok:
        subprocess.run(["rm", "-rf", folder_path], capture_output=True, timeout=300)
        return False, f"Failed to copy Telegram.app: {err}"

    # Patch Info.plist so the Dock shows the account name
    _patch_app_display_name(app_dest, name)

    # Bust the scan cache so the new account appears immediately
    invalidate_scan_cache()

    # Open Telegram so the user can log in
    if open_after:
        ok, msg = open_account(folder_path)
        if not ok:
            _log.error("create_account: failed to open %s after creation: %s", folder_path, msg)

    _log.info("Account created: %s", folder_path)
    return True, folder_path


def close_all():
    running = [acc for acc in scan_accounts() if acc["running"]]
    for acc in running:
        kill_account(acc["path"])

    def _cleanup_all():
        time.sleep(4)
        for acc in scan_accounts():
            remove_cloned_app(acc["path"])
        invalidate_scan_cache()
    threading.Thread(target=_cleanup_all, daemon=True).start()

def setup_account(folder_path):
    # Resolve app source: explicit config → shared master → any existing account copy
    app_source = config.get("app_source", "")
    if not os.path.isdir(app_source):
        app_source = get_shared_app() or ""
    if not os.path.isdir(app_source):
        for acc in scan_accounts_cached():
            candidate = find_account_app(acc["path"])
            if candidate and acc["path"] != folder_path:
                app_source = candidate
                break

    portable   = os.path.join(folder_path, "TelegramForcePortable")
    tdata_src  = os.path.join(folder_path, "tdata")
    tdata_dest = os.path.join(portable, "tdata")

    app_dest = find_account_app(folder_path)
    if not app_dest:
        if not os.path.isdir(app_source):
            return False, ("Telegram.app not found. Place Telegram.app in the data/ folder "
                           "next to TelegramManager.app, or set the path in Settings.")
        with _meta_lock:
            raw_name = metadata.get("dock_names", {}).get(folder_path) or os.path.basename(folder_path)
        safe_name = _safe_bundle_name(raw_name, os.path.basename(folder_path))
        app_dest = os.path.join(folder_path, safe_name + ".app")
        ok, err = _copy_app_bundle(app_source, app_dest)
        if not ok:
            return False, f"Failed to copy Telegram.app: {err}"

    os.makedirs(portable, exist_ok=True)

    if os.path.isdir(tdata_src) and not os.path.isdir(tdata_dest):
        r = subprocess.run(["mv", tdata_src, tdata_dest], capture_output=True, timeout=600)
        if r.returncode != 0:
            return False, f"Failed to move tdata: {r.stderr.decode(errors='replace').strip()}"

    for f in ("Telegram.exe", "Updater.exe", "log.txt", "log_start0.txt"):
        fp = os.path.join(folder_path, f)
        if os.path.exists(fp): os.remove(fp)
    modules = os.path.join(folder_path, "modules")
    if os.path.isdir(modules):
        subprocess.run(["rm", "-rf", modules], capture_output=True, timeout=300)

    # Set the Dock name to the account folder name
    account_name = os.path.basename(folder_path)
    set_telegram_display_name(folder_path, account_name)

    return True, "Setup complete"

def update_all_apps():
    running = [acc for acc in scan_accounts() if acc["running"]]
    if running:
        names = ", ".join(acc["name"] for acc in running[:5])
        if len(running) > 5:
            names += f" … (+{len(running) - 5} more)"
        return False, f"Close Telegram first for: {names}"

    # Resolve source: explicit config → fallback to newest per-account copy
    app_source = config.get("app_source", "")
    if not os.path.isdir(app_source):
        latest = _find_fallback_app_source(scan_accounts())
        if latest:
            app_source = latest

    shared = get_shared_app()
    if shared:
        if not os.path.isdir(app_source):
            return False, "Set Telegram.app source path in Settings first"
        type_err = _wrong_app_type_error(app_source)
        if type_err:
            return False, type_err
        shared_tmp = shared + ".new"
        subprocess.run(["rm", "-rf", shared_tmp], capture_output=True, timeout=300)
        r = subprocess.run(["cp", "-R", app_source, shared_tmp], capture_output=True, timeout=1800)
        if r.returncode != 0:
            subprocess.run(["rm", "-rf", shared_tmp], capture_output=True, timeout=300)
            return False, "Failed to update shared Telegram.app"
        subprocess.run(["rm", "-rf", shared], capture_output=True, timeout=300)
        os.rename(shared_tmp, shared)
        return True, "Shared Telegram.app updated"

    if not os.path.isdir(app_source):
        latest = _find_fallback_app_source(scan_accounts())
        if not latest:
            return False, "No Telegram app bundle found in any account"
        app_source = latest

    count = 0
    for acc in scan_accounts():
        app_dest = find_account_app(acc["path"])
        if os.path.isdir(os.path.join(acc["path"], "TelegramForcePortable")) and app_dest:
            if os.path.abspath(app_dest) == os.path.abspath(app_source):
                continue
            # Copy to a temp name, then swap — keeps the bundle name unchanged
            app_tmp = app_dest + ".new"
            subprocess.run(["rm", "-rf", app_tmp], capture_output=True, timeout=300)
            r = subprocess.run(["cp", "-R", app_source, app_tmp], capture_output=True, timeout=1800)
            if r.returncode != 0:
                subprocess.run(["rm", "-rf", app_tmp], capture_output=True, timeout=300)
                _log.warning("update_all_apps: cp failed for %s — skipping", acc["path"])
                continue
            subprocess.run(["rm", "-rf", app_dest], capture_output=True, timeout=300)
            os.rename(app_tmp, app_dest)
            # Re-apply dock name after binary update (Info.plist was overwritten by cp)
            with _meta_lock:
                dock_name = metadata.get("dock_names", {}).get(acc["path"]) or acc["name"]
            _patch_app_display_name(app_dest, dock_name)
            count += 1
    return True, f"Updated {count} accounts"


# ── Diagnose & Repair ─────────────────────────────────────────────────────

def diagnose_account(account_path):
    """
    Check an account folder for common problems that prevent Telegram from starting.
    Returns a dict with a list of issues and recommendations.
    """
    issues   = []
    warnings = []
    info     = []

    # ── 1. Basic folder structure ──────────────────────────────────────────
    portable = os.path.join(account_path, "TelegramForcePortable")
    tdata    = os.path.join(portable, "tdata")
    app      = find_account_app(account_path)

    if not os.path.isdir(account_path):
        issues.append({"id": "no_folder", "severity": "error",
                       "title": "Account folder missing",
                       "detail": f"The folder does not exist: {account_path}"})
        return {"issues": issues, "warnings": warnings, "info": info}

    if not app:
        if get_shared_app():
            info.append({"id": "shared_app", "severity": "info",
                         "title": "Uses shared Telegram.app",
                         "detail": "No Telegram.app in this folder — it will be APFS-cloned from the shared master on open. This is normal and saves disk space."})
        else:
            issues.append({"id": "no_app", "severity": "error",
                           "title": "Telegram.app missing",
                           "detail": "The Telegram.app was not found inside this account folder. "
                                     "Use Setup Account to copy it from the source, or set up a shared Telegram.app master."})

    if not os.path.isdir(portable):
        issues.append({"id": "no_portable", "severity": "error",
                       "title": "TelegramForcePortable folder missing",
                       "detail": "Telegram will not run in portable mode. "
                                 "Repair can recreate the folder."})

    if not os.path.isdir(tdata):
        issues.append({"id": "no_tdata", "severity": "error",
                       "title": "tdata folder missing",
                       "detail": "Session data is missing. This account has not been logged in, "
                                 "or the tdata was deleted. You will need to log in again."})
    else:
        # ── 2. tdata integrity: check for key files ────────────────────────
        key_file = os.path.join(tdata, "key_datas")
        if not os.path.exists(key_file):
            issues.append({"id": "no_key_datas", "severity": "error",
                           "title": "key_datas missing from tdata",
                           "detail": "The main session key file is absent — the session is corrupt "
                                     "or was never completed. You must log in again."})
        else:
            size = os.path.getsize(key_file)
            if size < 100:
                issues.append({"id": "key_datas_tiny", "severity": "error",
                               "title": "key_datas appears corrupt (too small)",
                               "detail": f"key_datas is only {size} bytes. "
                                         "The file is likely truncated from an interrupted write."})
            else:
                info.append(f"key_datas present ({size} bytes) ✓")

        # ── 3. Partial / zero-byte files in tdata ─────────────────────────
        bad_files = []
        try:
            for root_d, dirs, files in os.walk(tdata):
                for fname in files:
                    fpath = os.path.join(root_d, fname)
                    try:
                        if os.path.getsize(fpath) == 0 and not fname.endswith(".lock"):
                            bad_files.append(os.path.relpath(fpath, tdata))
                    except OSError:
                        pass
            if bad_files:
                warnings.append({"id": "zero_byte_files", "severity": "warning",
                                  "title": f"{len(bad_files)} zero-byte file(s) in tdata",
                                  "detail": "These may be partially written on a previous crash: "
                                            + ", ".join(bad_files[:5])
                                            + (" …" if len(bad_files) > 5 else ""),
                                  "fixable": True})
        except Exception as e:
            warnings.append({"id": "walk_error", "severity": "warning",
                              "title": "Could not fully inspect tdata",
                              "detail": str(e), "fixable": False})

        # ── 4. .lock files left by a crashed Telegram ─────────────────────
        lock_files = []
        try:
            for root_d, dirs, files in os.walk(tdata):
                for fname in files:
                    if fname.endswith(".lock"):
                        lock_files.append(os.path.relpath(os.path.join(root_d, fname), tdata))
        except Exception:
            pass
        if lock_files:
            issues.append({"id": "lock_files", "severity": "warning",
                           "title": f"{len(lock_files)} stale lock file(s) found",
                           "detail": "Lock files are left behind when Telegram crashes. "
                                     "Repair can remove them safely.",
                           "fixable": True,
                           "files": lock_files})
        else:
            info.append("No stale lock files ✓")

    # ── 5. Zombie Telegram process ─────────────────────────────────────────
    pid = find_telegram_pid(account_path)
    if pid:
        issues.append({"id": "zombie_process", "severity": "warning",
                       "title": f"Telegram is already running for this account (PID {pid})",
                       "detail": "A previous Telegram process is still alive and may be holding "
                                 "file locks. Repair can kill it.",
                       "fixable": True, "pid": pid})
    else:
        info.append("No running Telegram process for this account ✓")

    # ── 6. Settings / cache files that can be safely cleared ──────────────
    clearable = []
    for fname in ("settings0", "settings1", "user_data", "media_cache"):
        fpath = os.path.join(tdata, fname) if os.path.isdir(tdata) else None
        if fpath and os.path.exists(fpath):
            clearable.append(fname)

    # Telegram Desktop settings file (not tdata)
    settings_in_portable = os.path.join(portable, "settings")
    if os.path.exists(settings_in_portable):
        clearable.append("portable/settings")

    if clearable:
        warnings.append({"id": "clearable_cache", "severity": "info",
                          "title": "Cache / settings files can be cleared to fix startup crashes",
                          "detail": "These files do NOT contain your messages or contacts. "
                                    "Clearing them forces Telegram to rebuild them on next start: "
                                    + ", ".join(clearable),
                          "fixable": True, "files": clearable})

    # ── 7. Telegram.app binary check ──────────────────────────────────────
    if app and os.path.isdir(app):
        binary = os.path.join(app, "Contents", "MacOS", "Telegram")
        if not os.path.exists(binary):
            issues.append({"id": "no_binary", "severity": "error",
                           "title": "Telegram binary missing inside Telegram.app",
                           "detail": "The app bundle is incomplete. "
                                     "Use Setup Account to copy a fresh Telegram.app."})
        else:
            # Check it's executable
            if not os.access(binary, os.X_OK):
                issues.append({"id": "not_executable", "severity": "error",
                               "title": "Telegram binary is not executable",
                               "detail": "Repair can fix the file permissions.",
                               "fixable": True})
            else:
                info.append("Telegram binary present and executable ✓")

    return {"issues": issues, "warnings": warnings, "info": info}


def repair_account(account_path, actions):
    """
    Attempt to fix the account based on a list of requested repair actions.
    `actions` is a list of strings: e.g. ["kill_zombie", "remove_locks", "clear_cache", "fix_perms", "recreate_portable"]
    Returns a list of result dicts.
    """
    results = []
    portable = os.path.join(account_path, "TelegramForcePortable")
    tdata    = os.path.join(portable, "tdata")
    app      = find_account_app(account_path) or os.path.join(account_path, "Telegram.app")

    if "kill_zombie" in actions:
        pid = find_telegram_pid(account_path)
        if pid:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=10)
            time.sleep(1)
            results.append({"action": "kill_zombie", "ok": True,
                            "msg": f"Killed Telegram process (PID {pid})"})
        else:
            results.append({"action": "kill_zombie", "ok": True,
                            "msg": "No zombie process found (already gone)"})

    mutating_actions = {"remove_locks", "clear_cache", "fix_perms", "recreate_portable", "recopy_app"}
    if any(action in mutating_actions for action in actions):
        if os.path.isdir(tdata):
            ok, msg, backup_path = backup_account(account_path, os.path.basename(account_path))
            if ok:
                results.append({"action": "backup", "ok": True, "msg": f"{msg} ({backup_path})", "backup_path": backup_path})
            else:
                results.append({"action": "backup", "ok": False,
                                "msg": f"Pre-repair backup failed: {msg}"})
                return results
        else:
            results.append({"action": "backup", "ok": True,
                            "msg": "Backup skipped — no tdata found to snapshot"})

    if "remove_locks" in actions:
        removed = []
        errors  = []
        try:
            for root_d, dirs, files in os.walk(tdata):
                for fname in files:
                    if fname.endswith(".lock"):
                        fpath = os.path.join(root_d, fname)
                        try:
                            os.remove(fpath)
                            removed.append(fname)
                        except Exception as e:
                            errors.append(f"{fname}: {e}")
        except Exception as e:
            errors.append(str(e))
        if errors:
            results.append({"action": "remove_locks", "ok": False,
                            "msg": f"Removed {len(removed)} lock(s); errors: {'; '.join(errors)}"})
        else:
            results.append({"action": "remove_locks", "ok": True,
                            "msg": f"Removed {len(removed)} stale lock file(s)"})

    if "clear_cache" in actions:
        # Reuse the normal Clear Cache path instead of a second, narrower
        # target list — that hand-rolled version had no running-process
        # guard, so a client could clear cache on a live Telegram process
        # via /api/repair even though the equivalent normal action refuses to.
        if find_telegram_pid(account_path):
            results.append({"action": "clear_cache", "ok": False,
                            "msg": "Close Telegram for this account before clearing cache"})
        else:
            cleared, freed = clear_account_caches(account_path, threshold_mb=0)
            results.append({"action": "clear_cache", "ok": True,
                            "msg": f"Cleared {human_size(freed)}" if cleared else "Nothing to clear"})

    if "fix_perms" in actions:
        binary = os.path.join(app, "Contents", "MacOS", "Telegram")
        if os.path.exists(binary):
            try:
                os.chmod(binary, 0o755)
                # Also re-sign after permission fix
                r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app],
                                   capture_output=True, timeout=600)
                if r.returncode == 0:
                    results.append({"action": "fix_perms", "ok": True,
                                    "msg": "Fixed binary permissions and re-signed Telegram.app"})
                else:
                    results.append({"action": "fix_perms", "ok": False,
                                    "msg": "Fixed permissions but re-sign failed — "
                                           + r.stderr.decode(errors="replace").strip()})
            except Exception as e:
                results.append({"action": "fix_perms", "ok": False, "msg": str(e)})
        else:
            results.append({"action": "fix_perms", "ok": False,
                            "msg": "Binary not found — cannot fix permissions"})

    if "recreate_portable" in actions:
        try:
            os.makedirs(portable, exist_ok=True)
            results.append({"action": "recreate_portable", "ok": True,
                            "msg": "TelegramForcePortable folder recreated"})
        except Exception as e:
            results.append({"action": "recreate_portable", "ok": False, "msg": str(e)})

    if "recopy_app" in actions:
        app_source = config.get("app_source", "")
        if not os.path.isdir(app_source):
            app_source = get_shared_app() or ""
        if not os.path.isdir(app_source):
            results.append({"action": "recopy_app", "ok": False,
                            "msg": f"Source Telegram.app not found at {app_source}"})
        else:
            if os.path.isdir(app):
                subprocess.run(["rm", "-rf", app], capture_output=True, timeout=300)
            ok, err = _copy_app_bundle(app_source, app)
            if ok:
                # Re-apply dock name
                account_name = os.path.basename(account_path)
                set_telegram_display_name(account_path, account_name)
                results.append({"action": "recopy_app", "ok": True,
                                "msg": "Telegram.app replaced with a fresh copy"})
            else:
                results.append({"action": "recopy_app", "ok": False,
                                "msg": err or "cp failed"})

    return results


class RequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        # Last-resort guard: a handler bug or a subprocess timeout must yield a
        # JSON error, not an unhandled-exception traceback on a dead socket.
        try:
            self._do_GET()
        except Exception as e:
            _log.error("do_GET %s: unhandled error: %s", self.path, e, exc_info=True)
            try:
                self.send_json({"success": False, "message": f"Server error: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            _log.error("do_POST %s: unhandled error: %s", self.path, e, exc_info=True)
            try:
                self.send_json({"success": False, "message": f"Server error: {e}"}, 500)
            except Exception:
                pass

    def _do_GET(self):
        path = _route_path(self.path)
        if path is None:
            self.send_response(404)
            self.end_headers()
            return
        if path.startswith("/api/") and path not in _LOCK_EXEMPT and not _check_and_touch_unlocked():
            self.send_json({"success": False, "locked": True, "message": "App is locked"}, 423)
            return
        handler = self.GET_ROUTES.get(path)
        if handler:
            getattr(self, handler)()
        else:
            self.send_response(404)
            self.end_headers()

    def _get_api_accounts(self):
        accs = scan_accounts_cached()
        last_map = _last_backup_map()
        for a in accs:
            a["last_backup"] = last_map.get(a["name"], "")
        self.send_json(accs)

    def _get_api_config(self):
        # Never serialize the lock secrets — stripping here is defense in
        # depth (the endpoint is already gated while locked).
        safe = {k: v for k, v in config.items()
                if k not in ("lock_password_hash", "lock_password_salt")}
        safe["lock_enabled"] = bool(config.get("lock_password_hash"))
        self.send_json({**safe, "root_dir": ROOT_DIR,
                        "path_warnings": PATH_WARNINGS,
                        "reserved_names": sorted(SKIP_NAMES)})

    def _get_api_lock_status(self):
        # Ungated status probe used by the lock screen; must never touch
        # the activity timestamp (polling this endpoint must not itself
        # keep an idle session alive).
        self.send_json({
            "enabled":         _lock_enabled(),
            "unlocked":        _is_unlocked_no_touch(),
            "hint":            config.get("lock_hint", ""),
            "timeout_minutes": config.get("lock_timeout_minutes", 5),
        })

    def _get_api_backups(self):
        self.send_json(list_backups())

    def _get_api_workspaces(self):
        self.send_json(load_workspaces())

    def _get_api_keeper_status(self):
        self.send_json({**_keeper_status,
                        "enabled":       config.get("keeper_enabled", False),
                        "interval_days": config.get("keeper_interval_days", 30),
                        "open_seconds":  config.get("keeper_open_seconds", 120)})

    def _get_api_alerts(self):
        accs   = scan_accounts_cached()
        alerts = [
            {"path": a["path"], "name": a["name"],
             "issues": a["health"]["issues"], "expiry": a["health"]["expiry"],
             "health_status": a["health"]["status"]}
            for a in accs
            if a["health"]["expiry"] in ("stale", "expired")
               or a["health"]["status"] == "error"
        ]
        self.send_json(alerts)

    def _get_api_stats(self):
        accs        = scan_accounts_cached()
        disk        = get_disk_stats()
        tg_size     = disk.get("tg_size", 0)
        backup_size = disk.get("backup_size", 0)
        cache_total = disk.get("cache_total", 0)
        grand_total = tg_size + backup_size
        total_tdata = sum(a.get("tdata_size", 0) for a in accs)
        self.send_json({
            "total":              len(accs),
            "ready":              sum(1 for a in accs if a["status"] == "ready"),
            "running":            sum(1 for a in accs if a["running"]),
            "grand_total":        grand_total,
            "grand_total_human":  human_size(grand_total),
            "total_disk":         tg_size,
            "total_disk_human":   human_size(tg_size),
            "tdata_total":        total_tdata,
            "tdata_total_human":  human_size(total_tdata),
            "cache_total":        cache_total,
            "cache_total_human":  human_size(cache_total) if cache_total else "",
            "backup_size":        backup_size,
            "backup_size_human":  human_size(backup_size) if backup_size else "",
        })

    def _get_api_groups(self):
        self.send_json(list_groups())

    def _get_api_pick_app(self):
        picked, err = choose_app_dialog(
            "Select the Telegram.app to use as shared master")
        if picked:
            # Remember it as user-approved so /api/shared-app/setup will
            # accept it even though it may live outside the trusted roots.
            with _approved_sources_lock:
                _approved_app_sources.add(os.path.realpath(picked))
            self.send_json({"success": True, "path": picked})
        else:
            self.send_json({"success": False, "message": err})

    def _get_api_shared_app_status(self):
        shared = get_shared_app()
        accs   = scan_accounts_cached()
        own_apps = []
        total_bytes = 0
        for acc in accs:
            app_path = find_account_app(acc["path"])
            if app_path:
                sz = get_folder_size(app_path)
                total_bytes += sz
                own_apps.append({"path": acc["path"], "name": acc["name"],
                                 "size": sz, "size_human": human_size(sz)})
        # Determine what source setup would use (same priority as setup endpoint)
        cfg_source = config.get("app_source", "")
        if os.path.isdir(cfg_source):
            setup_source = cfg_source
            setup_source_type = "config"
        elif os.path.isdir(SIBLING_APP):
            setup_source = SIBLING_APP
            setup_source_type = "sibling"
        else:
            latest = _find_fallback_app_source(accs)
            setup_source = latest or ""
            setup_source_type = "account" if latest else "none"
        self.send_json({
            "shared_exists":      bool(shared),
            "shared_path":        shared or "",
            "own_apps":           own_apps,
            "own_count":          len(own_apps),
            "total_size":         total_bytes,
            "total_size_human":   human_size(total_bytes),
            "setup_source":       setup_source,
            "setup_source_type":  setup_source_type,
            "sibling_path":       SIBLING_APP,
        })

    def _get_api_logs(self):
        qs = parse_qs(urlparse(self.path).query)
        try:
            n_lines = int(qs.get("lines", ["200"])[0])
        except ValueError:
            n_lines = 200
        n_lines = max(1, min(1000, n_lines))
        if os.path.isfile(_log_file):
            with open(_log_file, "r", errors="replace") as f:
                lines = f.readlines()[-n_lines:]
            self.send_json({"lines": lines})
        else:
            self.send_json({"lines": []})

    def _get_root(self):
        self.serve_file("index.html", "text/html")

    def _do_POST(self):
        origin = self.headers.get("Origin", "")
        if origin and not (origin.startswith("http://127.0.0.1:") or
                           origin.startswith("http://localhost:")):
            self.send_json({"success": False, "message": "Forbidden"}, 403)
            return
        path   = _route_path(self.path)
        if path is None:
            self.send_json({"success": False, "message": "Not found"}, 404)
            return
        if path.startswith("/api/") and path not in _LOCK_EXEMPT and not _check_and_touch_unlocked():
            self.send_json({"success": False, "locked": True, "message": "App is locked"}, 423)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        body   = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        handler = self.POST_ROUTES.get(path)
        if handler:
            getattr(self, handler)(data)
        else:
            self.send_json({"success": False, "message": "Unknown endpoint"}, 404)

    def _post_api_open(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        ok, msg = open_account(acc_path)
        invalidate_scan_cache()
        if ok:
            self.send_json({"success": True})
        else:
            self.send_json({"success": False, "message": msg or "Failed to open account"})

    def _post_api_open_all(self, data):
        to_open = [acc for acc in scan_accounts()
                   if acc["status"] == "ready" and not acc["running"]]
        _open_accounts_async(to_open, "open-all")
        self.send_json({"success": True, "opened": len(to_open)})

    def _post_api_open_group(self, data):
        group   = data.get("group", "")
        to_open = [acc for acc in scan_accounts()
                   if acc["group"] == group and acc["status"] == "ready" and not acc["running"]]
        _open_accounts_async(to_open, "open-group")
        self.send_json({"success": True, "opened": len(to_open)})

    def _post_api_open_pinned(self, data):
        to_open = [acc for acc in scan_accounts()
                   if acc["pinned"] and acc["status"] == "ready" and not acc["running"]]
        _open_accounts_async(to_open, "open-pinned")
        self.send_json({"success": True, "opened": len(to_open)})

    def _post_api_close_all(self, data):
        close_all()
        self.send_json({"success": True})

    def _post_api_close_account(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        ok = kill_account(acc_path)
        if ok:
            self.send_json({"success": True})
        else:
            self.send_json({"success": False, "message": "Account is not running"})

    def _post_api_clear_cache(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        cleared, freed = clear_account_caches(acc_path)
        if cleared:
            invalidate_scan_cache()
            self.send_json({"success": True, "freed": freed,
                            "freed_human": human_size(freed),
                            "message": f"Cleared {human_size(freed)} of cache"})
        elif find_telegram_pid(acc_path):
            self.send_json({"success": False,
                            "message": "Close Telegram for this account before clearing its cache"})
        else:
            self.send_json({"success": True, "freed": 0,
                            "freed_human": "0 B", "message": "No cache to clear"})

    def _post_api_clear_all_cache(self, data):
        total_freed = cleared = skipped = 0
        for a in scan_accounts():
            if a.get("running"):
                skipped += 1
                continue
            ok, freed = clear_account_caches(a["path"])
            if ok and freed > 0:
                cleared += 1
                total_freed += freed
        invalidate_scan_cache()
        with _disk_stats_lock:
            _disk_stats["ts"] = 0.0   # force a fresh reclaimable-cache number
        msg = f"Cleared {human_size(total_freed)} across {cleared} account(s)"
        if skipped:
            msg += f"; skipped {skipped} running"
        self.send_json({"success": True, "freed": total_freed,
                        "freed_human": human_size(total_freed),
                        "cleared": cleared, "skipped": skipped, "message": msg})

    def _post_api_setup(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        ok, msg = setup_account(acc_path)
        self.send_json({"success": ok, "message": msg})

    def _post_api_setup_all(self, data):
        to_setup = [acc for acc in scan_accounts() if acc["status"] == "needs_setup"]
        def _setup_all(accounts_to_setup):
            for acc in accounts_to_setup:
                setup_account(acc["path"])
            invalidate_scan_cache()
        threading.Thread(target=_setup_all, args=(to_setup,), daemon=True).start()
        self.send_json({"success": True, "count": len(to_setup)})

    def _post_api_backup(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        ok, msg, backup_path = backup_account(acc_path, data.get("name", "account"))
        self.send_json({"success": ok, "message": msg, "backup_path": backup_path})

    def _post_api_backup_delete(self, data):
        ok, msg = delete_backup(data.get("backup_path", ""))
        if ok:
            invalidate_scan_cache()
        self.send_json({"success": ok, "message": msg})

    def _post_api_backup_all(self, data):
        to_backup = [acc for acc in scan_accounts() if acc["can_backup"]]
        success_count = 0
        failed = []
        for acc in to_backup:
            ok, msg, backup_path = backup_account(acc["path"], acc["name"])
            if ok:
                success_count += 1
            else:
                failed.append(f"{acc['name']}: {msg}")
        _log.info("backup-all: backed up %d/%d accounts", success_count, len(to_backup))
        response = {"success": True, "count": success_count, "total": len(to_backup)}
        if failed:
            response["message"] = f"Backed up {success_count}/{len(to_backup)} accounts; failed: {', '.join(failed[:3])}" + (" …" if len(failed) > 3 else "")
        else:
            response["message"] = f"Backed up {success_count} account{'' if success_count == 1 else 's'}"
        self.send_json(response)

    def _post_api_update_all(self, data):
        ok, msg = update_all_apps()
        self.send_json({"success": ok, "message": msg})

    def _post_api_note(self, data):
        acc_path = data.get("path", "")
        note     = data.get("note", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        with _meta_lock:
            metadata.setdefault("notes", {})[acc_path] = note
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_reorder(self, data):
        orders = data.get("orders", {})
        # A non-int order value persists into manager_data.json and then
        # crashes scan_accounts()'s sort() on every future call (int vs. str
        # comparison) — 500ing nearly every endpoint with no self-recovery.
        # _validate_import_payload enforces this same int-only rule for the
        # identical field; do it here too.
        if not isinstance(orders, dict) or not all(
            is_safe_path(k) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in orders.items()
        ):
            self.send_json({"success": False, "message": "Invalid path in order"}); return
        with _meta_lock:
            metadata.setdefault("order", {}).update(orders)
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_color(self, data):
        acc_path = data.get("path", "")
        color    = data.get("color", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        with _meta_lock:
            metadata.setdefault("colors", {})[acc_path] = color
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_username(self, data):
        acc_path = data.get("path", "")
        username = data.get("username", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        with _meta_lock:
            metadata.setdefault("usernames", {})[acc_path] = username
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_rename(self, data):
        old_path = data.get("path", "")
        new_name = data.get("new_name", "")
        # A non-string value (e.g. a client bug sending a number/bool) would
        # otherwise crash .strip() with an unhandled 500 instead of the clean
        # "Invalid name" response a same-shaped-but-empty value already gets.
        new_name = new_name.strip() if isinstance(new_name, str) else ""
        if not is_safe_path(old_path):
            self.send_json({"success": False, "message": "Invalid path"})
        elif (not new_name or "/" in new_name or new_name in (".", "..")
              or new_name in SKIP_NAMES or new_name.startswith(".")):
            # Same dot-check as create_account: scan_dir() skips any name
            # starting with "." so a dotfile rename would otherwise vanish
            # from the UI forever while the folder (and its tdata) stays
            # on disk, unreachable through the app.
            self.send_json({"success": False, "message": "Invalid name"})
        else:
            ok, result = rename_account(old_path, new_name)
            if ok:
                self.send_json({"success": True, "new_path": result})
            else:
                self.send_json({"success": False, "message": result})

    def _post_api_pin(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        with _meta_lock:
            pinned_list = metadata.setdefault("pinned", [])
            if acc_path in pinned_list:
                pinned_list.remove(acc_path)
                is_pinned = False
            else:
                pinned_list.append(acc_path)
                is_pinned = True
            save_metadata(metadata)
        self.send_json({"success": True, "pinned": is_pinned})

    def _post_api_proxy(self, data):
        acc_path = data.get("path", "")
        proxy    = data.get("proxy")   # dict with type/host/port/user/pass, or null to clear
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        # A non-dict proxy crashes proxy.get("host") below with an unhandled
        # 500; a dict with a wrong-typed field (e.g. non-string "type") is
        # stored as-is and later crashes apply_proxy() the first time this
        # account is opened. _validate_import_payload enforces this same
        # shape for the import path — apply the identical rule here.
        if proxy is not None:
            if not isinstance(proxy, dict):
                self.send_json({"success": False, "message": "Invalid proxy"}); return
            for k in ("type", "host", "user", "pass"):
                if proxy.get(k) is not None and not isinstance(proxy.get(k), str):
                    self.send_json({"success": False, "message": "Invalid proxy"}); return
            if proxy.get("port") is not None and not isinstance(proxy.get("port"), int):
                self.send_json({"success": False, "message": "Invalid proxy"}); return
        with _meta_lock:
            if proxy and proxy.get("host"):
                metadata.setdefault("proxies", {})[acc_path] = proxy
            else:
                metadata.get("proxies", {}).pop(acc_path, None)
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_dock_name(self, data):
        acc_path  = data.get("path", "")
        dock_name = data.get("dock_name", "")
        dock_name = dock_name.strip() if isinstance(dock_name, str) else ""
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        if is_running(acc_path):
            self.send_json({"success": False, "message": "Close Telegram first"})
            return
        # Save to metadata
        with _meta_lock:
            if dock_name:
                metadata.setdefault("dock_names", {})[acc_path] = dock_name
            else:
                metadata.get("dock_names", {}).pop(acc_path, None)
            save_metadata(metadata)
        # Apply immediately: rename bundle + patch Info.plist
        existing_app = find_account_app(acc_path)
        effective_name = dock_name or os.path.basename(acc_path)
        if existing_app:
            safe_bundle = _safe_bundle_name(effective_name, os.path.basename(acc_path))
            new_app_path = os.path.join(acc_path, safe_bundle + ".app")
            if os.path.abspath(existing_app) != os.path.abspath(new_app_path):
                try:
                    os.rename(existing_app, new_app_path)
                    _log.info("Renamed app bundle %s → %s", existing_app, new_app_path)
                except Exception as e:
                    self.send_json({"success": False, "message": f"Could not rename app bundle: {e}"})
                    return
            _patch_app_display_name(new_app_path, effective_name)
        invalidate_scan_cache()
        self.send_json({"success": True})

    def _post_api_set_avatar(self, data):
        acc_path = data.get("path", "")
        image    = data.get("image", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        if image:
            prefix = None
            for p in ("data:image/jpeg;base64,", "data:image/png;base64,"):
                if image.startswith(p):
                    prefix = p
                    break
            if prefix is None:
                self.send_json({"success": False, "message": "Invalid image"}); return
            try:
                decoded = base64.b64decode(image[len(prefix):], validate=True)
            except Exception:
                self.send_json({"success": False, "message": "Invalid image"}); return
            if len(decoded) > 300_000:
                self.send_json({"success": False, "message": "Image too large"}); return
        with _meta_lock:
            if image:
                metadata.setdefault("avatars", {})[acc_path] = image
            else:
                metadata.get("avatars", {}).pop(acc_path, None)
            save_metadata(metadata)
        self.send_json({"success": True})

    def _post_api_restore(self, data):
        backup_path  = data.get("backup_path", "")
        account_path = data.get("account_path", "")
        if not is_safe_path(backup_path) or not is_safe_path(account_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        ok, msg = restore_backup(backup_path, account_path)
        self.send_json({"success": ok, "message": msg})

    def _post_api_workspace_save(self, data):
        name          = data.get("name", "")
        name          = name.strip() if isinstance(name, str) else ""
        accounts_list = data.get("accounts", [])
        icon          = data.get("icon", "📁")
        if not name or not accounts_list:
            self.send_json({"success": False, "message": "Name and accounts required"})
        elif len(name) > 64:
            self.send_json({"success": False, "message": "Workspace name too long (max 64 chars)"})
        elif not isinstance(accounts_list, list) or not all(is_safe_path(p) for p in accounts_list):
            self.send_json({"success": False, "message": "Invalid account path in workspace"})
        else:
            with _ws_lock:
                ws = load_workspaces()
                ws[name] = {"accounts": accounts_list, "icon": icon,
                            "created": datetime.now().isoformat()}
                save_workspaces(ws)
            self.send_json({"success": True})

    def _post_api_workspace_open(self, data):
        name = data.get("name", "")
        ws   = load_workspaces()
        if name not in ws:
            self.send_json({"success": False, "message": "Workspace not found"})
        else:
            accs    = {a["path"]: a for a in scan_accounts()}
            to_open = [accs[p] for p in ws[name].get("accounts", [])
                       if p in accs and accs[p]["status"] == "ready"
                       and not accs[p]["running"]]
            _open_accounts_async(to_open, "open-workspace")
            self.send_json({"success": True, "opened": len(to_open)})

    def _post_api_workspace_delete(self, data):
        name = data.get("name", "")
        with _ws_lock:
            ws = load_workspaces()
            ws.pop(name, None)
            save_workspaces(ws)
        self.send_json({"success": True})

    def _post_api_keeper_run_now(self, data):
        started, msg = trigger_keeper_now()
        self.send_json({"success": started, "message": msg})

    def _post_api_fix_dock_names(self, data):
        acc_path = data.get("path")  # optional: fix just one account
        if acc_path:
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            acc_name = data.get("name", os.path.basename(acc_path))
            ok, msg  = set_telegram_display_name(acc_path, acc_name)
            self.send_json({"success": ok, "message": msg})
        else:
            results = fix_all_dock_names()
            fixed   = sum(1 for r in results if r["ok"])
            self.send_json({"success": True, "fixed": fixed, "results": results})

    def _post_api_reveal(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        if not os.path.isdir(acc_path):
            self.send_json({"success": False, "message": "Folder not found"})
            return
        subprocess.Popen(["open", "-R", acc_path])
        self.send_json({"success": True})

    def _post_api_delete(self, data):
        acc_path = data.get("path", "")
        if not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        if not acc_path or not os.path.isdir(acc_path):
            self.send_json({"success": False, "message": "Folder not found"})
        elif is_running(acc_path):
            self.send_json({"success": False, "message": "Telegram is still running for this account — close it first"})
        else:
            try:
                tdata_path = os.path.join(acc_path, "TelegramForcePortable", "tdata")
                backup_path = ""
                if os.path.isdir(tdata_path):
                    ok, msg, backup_path = backup_account(acc_path, os.path.basename(acc_path))
                    if not ok:
                        self.send_json({"success": False, "message": f"Backup before delete failed: {msg}"})
                        return
                # Move to Trash via Finder — safe, recoverable
                script = f'tell application "Finder" to move (POSIX file {_as_str(acc_path)}) to trash'
                r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    self.send_json({"success": False, "message": r.stderr.strip() or "Move to Trash failed"})
                else:
                    # Clean up all metadata for this path
                    with _meta_lock:
                        for section in ("notes", "usernames", "order",
                                        "colors", "last_opened", "dock_names",
                                        "proxies", "avatars"):
                            metadata.get(section, {}).pop(acc_path, None)
                        pinned = metadata.get("pinned", [])
                        if acc_path in pinned:
                            pinned.remove(acc_path)
                        save_metadata(metadata)
                    msg = "Moved to Trash"
                    if backup_path:
                        msg += f"; backup saved at {backup_path}"
                    self.send_json({"success": True, "message": msg, "backup_path": backup_path})
            except Exception as e:
                self.send_json({"success": False, "message": str(e)})

    def _post_api_create_account(self, data):
        name        = data.get("name", "")
        name        = name.strip() if isinstance(name, str) else ""
        # Default "" (not ROOT_DIR) — create_account() treats a falsy
        # parent_path as "top level" and resolves it internally; ROOT_DIR
        # itself always fails is_safe_path() by design (root/DATA_DIR are
        # rejected, see state.is_safe_path), so defaulting to it here made
        # any caller that omits parent_path always get "Invalid path".
        parent_path = data.get("parent_path", "")
        open_after  = data.get("open_after", True)
        if parent_path and not is_safe_path(parent_path):
            self.send_json({"success": False, "message": "Invalid path"}); return
        ok, result  = create_account(name, parent_path, open_after)
        if ok:
            self.send_json({"success": True, "path": result})
        else:
            self.send_json({"success": False, "message": result})

    def _post_api_diagnose(self, data):
        try:
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            if not acc_path:
                self.send_json({"success": False, "message": "path required"})
                return
            result = diagnose_account(acc_path)
            self.send_json({"success": True, **result})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def _post_api_repair(self, data):
        try:
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            actions  = data.get("actions", [])
            if not acc_path or not actions:
                self.send_json({"success": False, "message": "path and actions required"})
                return
            results = repair_account(acc_path, actions)
            self.send_json({"success": True, "results": results})
        except Exception as e:
            self.send_json({"success": False, "message": str(e)})

    def _post_api_unlock(self, data):
        if not _lock_enabled():
            self.send_json({"success": True, "enabled": False})
            return
        if _verify_lock_password(data.get("password")):
            _server_unlock()
            self.send_json({"success": True})
        else:
            fail_count = _register_unlock_failure()
            _log.warning("unlock: incorrect password (consecutive failures=%d)", fail_count)
            time.sleep(min(0.5 * fail_count, 5.0))
            self.send_json({"success": False, "message": "Incorrect password"}, 403)

    def _post_api_lock(self, data):
        _server_lock()
        self.send_json({"success": True})

    def _post_api_lock_config(self, data):
        # The only way to set/change/remove the lock password. A
        # currently-unlocked session is deliberately insufficient to
        # change or remove an existing password — the caller must prove
        # they know the current one, so a walked-away unlocked screen
        # can't silently drop the lock.
        if _lock_enabled():
            if not _verify_lock_password(data.get("current_password")):
                fail_count = _register_unlock_failure()
                _log.warning("lock-config: incorrect current password (consecutive failures=%d)", fail_count)
                time.sleep(min(0.5 * fail_count, 5.0))
                self.send_json({"success": False, "message": "Current password is incorrect"}, 403)
                return

        new_pw  = data.get("new_password")
        hint    = data.get("hint")
        timeout = data.get("timeout_minutes")

        if new_pw is not None and (not isinstance(new_pw, str) or not new_pw):
            self.send_json({"success": False, "message": "New password cannot be empty"}, 400)
            return

        with _config_lock:
            if new_pw:
                salt = secrets.token_hex(16)
                config["lock_password_hash"] = hashlib.sha256((salt + new_pw).encode()).hexdigest()
                config["lock_password_salt"] = salt
            else:
                # new_password null/absent → remove the lock entirely.
                config["lock_password_hash"] = None
                config["lock_password_salt"] = None
            if isinstance(hint, str):
                config["lock_hint"] = hint
            if isinstance(timeout, int) and not isinstance(timeout, bool):
                config["lock_timeout_minutes"] = max(0, timeout)
            save_config(config)

        # The person who just proved the (old or new) password is
        # standing there — don't make them re-enter it immediately.
        _server_unlock()
        self.send_json({"success": True, "lock_enabled": bool(config.get("lock_password_hash"))})

    def _post_api_config(self, data):
        int_keys  = ("keeper_interval_days", "keeper_open_seconds",
                     "auto_clear_cache_mb", "backup_keep_per_account",
                     "lock_timeout_minutes")
        bool_keys = ("keeper_enabled", "proxy_system_apply")
        rejected  = []   # keys silently dropping a value would hide real errors from the UI
        with _config_lock:
            for k in ("app_source", "extra_scan_dirs",
                      "keeper_enabled", "keeper_interval_days", "keeper_open_seconds",
                      "auto_clear_cache_mb", "proxy_system_apply",
                      "backup_keep_per_account",
                      "lock_hint", "lock_timeout_minutes"):
                if k not in data:
                    continue
                v = data[k]
                # Coerce/reject wrong JSON types so a bad client can't
                # persist e.g. a string where int is expected.
                if k in bool_keys:
                    v = bool(v)
                elif k in int_keys:
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        _log.warning("config: ignoring non-integer %s=%r", k, v)
                        rejected.append(f"{k}: not a number")
                        continue
                    if k in _INT_KEY_BOUNDS:
                        lo, hi = _INT_KEY_BOUNDS[k]
                        v = max(lo, min(hi, v))
                elif k == "app_source":
                    if v and not isinstance(v, str):
                        _log.warning("config: ignoring non-string app_source=%r", v)
                        rejected.append("app_source: must be a string")
                        continue
                    if v and not is_allowed_app_source(v):
                        # app_source is later copied + quarantine-stripped + launched,
                        # so a token-only caller must not be able to set it to an
                        # arbitrary path. Empty = clear (falls back to auto-detect).
                        _log.warning("config: rejecting untrusted app_source=%r", v)
                        rejected.append("app_source: path is not in a trusted location "
                                        "(/Applications, the accounts folder, or data/) — "
                                        "use “Choose App…” in Advanced → Shared App instead")
                        continue
                elif k == "extra_scan_dirs":
                    if not isinstance(v, list) or not all(isinstance(d, str) for d in v):
                        # Stored as-is otherwise, scan_accounts() would later
                        # call os.path.expanduser() on a non-string entry and
                        # crash every future scan with no self-recovery.
                        _log.warning("config: ignoring malformed extra_scan_dirs=%r", v)
                        rejected.append("extra_scan_dirs: must be an array of strings")
                        continue
                config[k] = v
            save_config(config)
        invalidate_scan_cache()
        # Validate extra_scan_dirs and report any that don't exist
        bad_dirs = [
            d for d in config.get("extra_scan_dirs", [])
            if d and not os.path.isdir(os.path.expanduser(d))
        ]
        self.send_json({"success": True, "bad_dirs": bad_dirs, "rejected": rejected})


    def _post_api_shared_app_setup(self, data):
        # Copy best available Telegram.app to _apps/macOS/Telegram.app
        # Priority: explicit request source → config → sibling ROOT_DIR/Telegram.app → newest per-account copy
        req_source = data.get("source", "")
        if req_source:
            if not is_allowed_app_source(req_source):
                self.send_json({"success": False,
                                "message": "Source is not a valid or approved .app bundle. "
                                           "Use “Choose App…” to select it."})
                return
            app_source = req_source
        else:
            app_source = config.get("app_source", "")
        if not os.path.isdir(app_source):
            # Auto-detect: Telegram.app placed next to TelegramManager.app
            if os.path.isdir(SIBLING_APP):
                app_source = SIBLING_APP
            else:
                latest = _find_fallback_app_source(scan_accounts())
                if not latest:
                    self.send_json({"success": False,
                                    "message": "No Telegram.app found. Place Telegram.app next to TelegramManager.app, or set the app path in Settings."})
                    return
                app_source = latest
        # Defense in depth: whatever the resolution path, never copy+launch
        # a bundle from an untrusted location.
        if not is_allowed_app_source(app_source):
            self.send_json({"success": False,
                            "message": "Resolved Telegram.app source is not in a trusted "
                                       "location. Use “Choose App…” to select it."})
            return
        type_err = _wrong_app_type_error(app_source)
        if type_err:
            self.send_json({"success": False, "message": type_err})
            return
        shared_dir = os.path.join(SHARED_APPS_DIR, "macOS")
        os.makedirs(shared_dir, exist_ok=True)
        dest = os.path.join(shared_dir, "Telegram.app")
        if os.path.isdir(dest):
            subprocess.run(["rm", "-rf", dest], capture_output=True, timeout=300)
        r = subprocess.run(["cp", "-R", app_source, dest], capture_output=True, timeout=1800)
        if r.returncode != 0:
            self.send_json({"success": False, "message": "Copy failed: " + r.stderr.decode()})
            return
        # Strip quarantine so the master (and every clone from it) launches without Gatekeeper prompts
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", dest], capture_output=True, timeout=120)
        invalidate_scan_cache()
        self.send_json({"success": True,
                        "message": "Shared Telegram.app is ready. Accounts will use it on next open."})

    def _post_api_shared_app_remove_account_app(self, data):
        acc_path = data.get("path", "")
        if not acc_path or not is_safe_path(acc_path):
            self.send_json({"success": False, "message": "Invalid path"})
            return
        app_path = find_account_app(acc_path)
        if not app_path:
            self.send_json({"success": False, "message": "No Telegram app bundle found in this folder"})
            return
        sz = get_folder_size(app_path)
        subprocess.run(["rm", "-rf", app_path], capture_output=True, timeout=300)
        invalidate_scan_cache()
        self.send_json({"success": True, "freed": sz, "freed_human": human_size(sz)})

    def _post_api_shared_app_remove_all_account_apps(self, data):
        if not get_shared_app():
            self.send_json({"success": False,
                            "message": "Set up the shared Telegram.app first before removing per-account copies."})
            return
        removed = 0
        freed   = 0
        for acc in scan_accounts():
            app_path = find_account_app(acc["path"])
            if app_path:
                freed += get_folder_size(app_path)
                subprocess.run(["rm", "-rf", app_path], capture_output=True, timeout=300)
                removed += 1
        invalidate_scan_cache()
        self.send_json({"success": True, "removed": removed,
                        "freed": freed, "freed_human": human_size(freed)})

    def _post_api_export_config(self, data):
        # Strip the lock secrets from the export — otherwise export
        # re-opens the offline brute-force hole this whole feature closes.
        exported_cfg = {k: v for k, v in load_config().items()
                        if k not in ("lock_password_hash", "lock_password_salt")}
        self.send_json({
            "version":    1,
            "exported_at": datetime.now().isoformat(),
            "metadata":   load_metadata(),
            "config":     exported_cfg,
            "workspaces": load_workspaces(),
        })

    def _post_api_import_config(self, data):
        ok, message, normalized = _validate_import_payload(data)
        if not ok:
            self.send_json({"success": False, "message": message})
            return
        imported_cfg = normalized["config"]
        # Import can never alter the lock password — it's only ever set
        # via /api/lock-config (which requires proving the current one).
        # Keep the two keys accepted by _validate_import_payload (so old
        # export files still validate) but overwrite with live values.
        imported_cfg["lock_password_hash"] = config.get("lock_password_hash")
        imported_cfg["lock_password_salt"] = config.get("lock_password_salt")
        try:
            with _meta_lock:
                save_metadata(normalized["metadata"])
                metadata.clear()
                metadata.update(normalized["metadata"])
            with _config_lock:
                save_config(imported_cfg)
                config.clear()
                config.update(imported_cfg)
            save_workspaces(normalized["workspaces"])
            invalidate_scan_cache()
            _log.info("Config imported from client")
            self.send_json({"success": True, "message": "Config imported successfully"})
        except Exception as e:
            _log.warning("import-config failed: %s", e)
            self.send_json({"success": False, "message": str(e)})


    GET_ROUTES = {
        '/api/accounts': '_get_api_accounts',
        '/api/config': '_get_api_config',
        '/api/lock-status': '_get_api_lock_status',
        '/api/backups': '_get_api_backups',
        '/api/workspaces': '_get_api_workspaces',
        '/api/keeper/status': '_get_api_keeper_status',
        '/api/alerts': '_get_api_alerts',
        '/api/stats': '_get_api_stats',
        '/api/groups': '_get_api_groups',
        '/api/pick-app': '_get_api_pick_app',
        '/api/shared-app/status': '_get_api_shared_app_status',
        '/api/logs': '_get_api_logs',
        '/': '_get_root',
        '/index.html': '_get_root',
    }

    POST_ROUTES = {
        '/api/open': '_post_api_open',
        '/api/open-all': '_post_api_open_all',
        '/api/open-group': '_post_api_open_group',
        '/api/open-pinned': '_post_api_open_pinned',
        '/api/close-all': '_post_api_close_all',
        '/api/close-account': '_post_api_close_account',
        '/api/clear-cache': '_post_api_clear_cache',
        '/api/clear-all-cache': '_post_api_clear_all_cache',
        '/api/setup': '_post_api_setup',
        '/api/setup-all': '_post_api_setup_all',
        '/api/backup': '_post_api_backup',
        '/api/backup/delete': '_post_api_backup_delete',
        '/api/backup-all': '_post_api_backup_all',
        '/api/update-all': '_post_api_update_all',
        '/api/note': '_post_api_note',
        '/api/reorder': '_post_api_reorder',
        '/api/color': '_post_api_color',
        '/api/username': '_post_api_username',
        '/api/rename': '_post_api_rename',
        '/api/pin': '_post_api_pin',
        '/api/proxy': '_post_api_proxy',
        '/api/dock-name': '_post_api_dock_name',
        '/api/set-avatar': '_post_api_set_avatar',
        '/api/restore': '_post_api_restore',
        '/api/workspace/save': '_post_api_workspace_save',
        '/api/workspace/open': '_post_api_workspace_open',
        '/api/workspace/delete': '_post_api_workspace_delete',
        '/api/keeper/run-now': '_post_api_keeper_run_now',
        '/api/fix-dock-names': '_post_api_fix_dock_names',
        '/api/reveal': '_post_api_reveal',
        '/api/delete': '_post_api_delete',
        '/api/create-account': '_post_api_create_account',
        '/api/diagnose': '_post_api_diagnose',
        '/api/repair': '_post_api_repair',
        '/api/unlock': '_post_api_unlock',
        '/api/lock': '_post_api_lock',
        '/api/lock-config': '_post_api_lock_config',
        '/api/config': '_post_api_config',
        '/api/shared-app/setup': '_post_api_shared_app_setup',
        '/api/shared-app/remove-account-app': '_post_api_shared_app_remove_account_app',
        '/api/shared-app/remove-all-account-apps': '_post_api_shared_app_remove_all_account_apps',
        '/api/export-config': '_post_api_export_config',
        '/api/import-config': '_post_api_import_config',
    }


    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, filename, content_type):
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        msg = fmt % args
        # The request line embeds ROUTE_PREFIX (the secret URL token); redact it
        # so the token never lands in manager.log.
        if ROUTE_PREFIX:
            msg = msg.replace(ROUTE_PREFIX + "/", "/").replace(ROUTE_PREFIX, "")
        _log.info("%s - %s", self.address_string(), msg)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so slow scans don't block."""
    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=run_keeper_loop, daemon=True).start()
    threading.Thread(target=_app_watcher_loop, daemon=True).start()
    threading.Thread(target=_recover_stale_proxy, daemon=True).start()

    port = config.get("port", 8477)
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        _log.warning("Invalid port %r in config, falling back to 8477", port)
        port = 8477
    _log.info("Server starting on 127.0.0.1:%d  (ROOT_DIR=%s)", port, ROOT_DIR)
    for w in PATH_WARNINGS:
        _log.warning("PATH FALLBACK: %s", w)
    if _TOKEN_GENERATED:
        # Manual/standalone run — token exists only in this process, so print
        # the full URL to stdout for the developer (never to the log file).
        print(f"Session token generated. UI: http://127.0.0.1:{port}{ROUTE_PREFIX}/")
    server = ThreadedHTTPServer(("127.0.0.1", port), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _log.info("Server shutting down")
