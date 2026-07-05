#!/usr/bin/env python3
"""Telegram Manager - Backend Server v2"""

import copy
import json
import os
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import logging
from logging.handlers import RotatingFileHandler

# Layout (clean-root mode):
#   PARENT/
#     TelegramManager.app/   ← APP_BUNDLE
#     TelegramManager Lite.app/
#     TG/                    ← ROOT_DIR  — account folders only
#     data/                  ← DATA_DIR  — config, logs, backups, avatars, _apps
#
# Falls back to PARENT for both ROOT_DIR and DATA_DIR if the subfolders don't exist.
APP_BUNDLE       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PARENT_DIR      = os.path.dirname(APP_BUNDLE)  # true parent (Swift launcher reads config here)
_tg_sub          = os.path.join(_PARENT_DIR, "TG")
ROOT_DIR         = _tg_sub if os.path.isdir(_tg_sub) else _PARENT_DIR
_data_sub        = os.path.join(_PARENT_DIR, "data")
DATA_DIR         = _data_sub if os.path.isdir(_data_sub) else _PARENT_DIR
PRIVATE_STATE_DIR = os.path.expanduser("~/Library/Application Support/TelegramManager")
METADATA_FILE    = os.path.join(PRIVATE_STATE_DIR, "manager_data.json")
CONFIG_FILE      = os.path.join(DATA_DIR, "manager_config.json")
WORKSPACES_FILE  = os.path.join(PRIVATE_STATE_DIR, "manager_workspaces.json")
LEGACY_METADATA_FILE   = os.path.join(DATA_DIR, "manager_data.json")
LEGACY_WORKSPACES_FILE = os.path.join(DATA_DIR, "manager_workspaces.json")

try:
    os.makedirs(PRIVATE_STATE_DIR, exist_ok=True)
    os.chmod(PRIVATE_STATE_DIR, 0o700)
except OSError:
    pass

# ── Logging ──────────────────────────────────────────────────────────────────
_log_file    = os.path.join(DATA_DIR, "manager.log")
_log         = logging.getLogger("TelegramManager")
_log.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3,
                                   encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_log_handler)
# The log line records request paths, which contain the secret URL token.
# Keep it owner-only so another local user/process can't read the token back.
try:
    os.chmod(_log_file, 0o600)
except OSError:
    pass

DEFAULT_CONFIG = {
    "app_source":            "",
    "port":                  8477,
    "extra_scan_dirs":       [],
    # Session Keeper
    "keeper_enabled":        False,
    "keeper_interval_days":  30,
    "keeper_open_seconds":   120,
    # Media cache: auto-clear on close if cache exceeds this size (MB). 0 = disabled.
    "auto_clear_cache_mb":   0,
    # After each new backup keep only the newest N per account. 0 = keep all.
    "backup_keep_per_account": 0,
    # Apply account proxy system-wide when opening (admin prompt)
    "proxy_system_apply":    False,
    # Password lock
    "lock_password_hash":    None,
    "lock_password_salt":    None,
    "lock_hint":             "",
    "lock_timeout_minutes":  5,
}

def _sq(value: str) -> str:
    """Single-quote a shell argument for embedding inside an AppleScript double-quoted string.

    shlex.quote() may emit double-quoted strings which break AppleScript's own double-quote
    delimiters. This always produces single-quoted strings by escaping embedded single quotes
    as: '  →  '\''
    """
    return "'" + str(value).replace("'", "'\\''") + "'"

def _as_str(value: str) -> str:
    """Produce an AppleScript string expression for value, safe for use in any string context.

    AppleScript has no backslash escape sequences inside string literals — a literal " cannot
    be embedded directly.  We split on " and rejoin with (ASCII character 34) concatenation:
        /foo/bar         →  "/foo/bar"
        /foo "bar"/baz   →  "/foo " & (ASCII character 34) & "bar" & (ASCII character 34) & "/baz"
    The result is a valid AppleScript expression that evaluates to the original string.
    """
    parts = str(value).split('"')
    return '"' + ('" & (ASCII character 34) & "'.join(parts)) + '"'

def _run_as_admin(shell_cmds: str, prompt: str) -> "subprocess.CompletedProcess[str]":
    """Run shell_cmds with macOS administrator privileges via osascript.

    Writes the commands to a temp file so no user-controlled value is ever embedded inside
    the AppleScript 'do shell script "..."' string literal — which _sq() alone cannot
    guarantee because single-quoted shell strings may still contain '"' characters.
    """
    import tempfile, stat
    fd, tmp_path = tempfile.mkstemp(suffix=".sh", prefix="tm_admin_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(shell_cmds)
        os.chmod(tmp_path, stat.S_IRWXU)
        prompt_expr = _as_str(prompt)
        script = (
            f'do shell script "bash {shlex.quote(tmp_path)}" '
            f'with administrator privileges with prompt {prompt_expr}'
        )
        try:
            return subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            _log.warning("_run_as_admin: osascript timed out after 120s")
            return subprocess.CompletedProcess(
                ["osascript"], returncode=124, stdout="",
                stderr="administrator prompt timed out")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def choose_app_dialog(prompt_text, default_dir="/Applications", timeout=600):
    """Show a native macOS file picker restricted to .app bundles.

    Returns (posix_path, "") on selection, (None, "canceled") if the user
    cancels, (None, error_message) on any other failure. The AppleScript
    source contains only values escaped through _as_str() — never raw input.
    """
    lines = ["tell me to activate"]
    choose = (
        'set f to choose file of type {"com.apple.application-bundle"}'
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


def _save_json_atomic(path, value):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_json_file(path, default_value):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        _log.warning("Failed to load JSON from %s: %s", path, e)
    return copy.deepcopy(default_value)


def _load_json_file_with_fallbacks(path, default_value, fallback_paths=()):
    for candidate in (path, *fallback_paths):
        try:
            if os.path.exists(candidate):
                with open(candidate) as f:
                    return json.load(f), candidate
        except Exception as e:
            _log.warning("Failed to load JSON from %s: %s", candidate, e)
    return copy.deepcopy(default_value), None

def is_safe_path(path: str) -> bool:
    """Return True if path resolves to within ROOT_DIR or DATA_DIR (prevents path traversal)."""
    if not path:
        return False
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        for base in (ROOT_DIR, DATA_DIR):
            real_base = os.path.realpath(base)
            if real_path == real_base or real_path.startswith(real_base + os.sep):
                return True
        return False
    except Exception:
        return False

def load_config():
    cfg = _load_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

def save_config(cfg):
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    # Mirror port to true parent dir so the Swift launcher always finds it (atomic write)
    if DATA_DIR != _PARENT_DIR:
        try:
            mirror = os.path.join(_PARENT_DIR, "manager_config.json")
            mirror_tmp = mirror + ".tmp"
            with open(mirror_tmp, "w") as f:
                json.dump({"port": cfg.get("port", 8477)}, f)
            os.replace(mirror_tmp, mirror)
        except OSError:
            pass

def load_metadata():
    data, source = _load_json_file_with_fallbacks(
        METADATA_FILE,
        {"notes": {}, "usernames": {}, "order": {}, "colors": {},
         "last_opened": {}, "pinned": [], "proxies": {}},
        (LEGACY_METADATA_FILE,)
    )
    if source == LEGACY_METADATA_FILE and METADATA_FILE != LEGACY_METADATA_FILE:
        try:
            _save_json_atomic(METADATA_FILE, data)
            os.unlink(LEGACY_METADATA_FILE)
        except OSError:
            pass
    return data

# Thread locks for file I/O.
# Both are RLocks so handlers can acquire them and then call save_metadata() /
# save_config() (which also acquire them) without deadlocking.
_meta_lock   = threading.RLock()
_config_lock = threading.RLock()
# RLock so an endpoint can hold it across load_workspaces→mutate→save_workspaces
# while save_workspaces() re-acquires it internally without deadlocking.
_ws_lock     = threading.RLock()

def save_metadata(meta):
    """Atomic write: write to .tmp then rename, so a crash can't corrupt the file."""
    with _meta_lock:
        _save_json_atomic(METADATA_FILE, meta)


config   = load_config()
metadata = load_metadata()
SESSION_TOKEN = os.environ.get("TG_SESSION_TOKEN", "")
ROUTE_PREFIX  = f"/{SESSION_TOKEN}" if SESSION_TOKEN else ""

def _route_path(raw_path: str):
    path = urlparse(raw_path).path
    if not ROUTE_PREFIX:
        return path
    if path == ROUTE_PREFIX or path == ROUTE_PREFIX + "/":
        return "/"
    if path.startswith(ROUTE_PREFIX + "/"):
        return path[len(ROUTE_PREFIX):]
    return None

SKIP_NAMES = {
    "TelegramForcePortable", "Backups", ".DS_Store",
    "TelegramManager.app", "Telegram.app", "tupdates", "tdata", "modules", "_apps"
}

# ── Shared App (single master, APFS-cloned per account on open) ────────────
SHARED_APPS_DIR  = os.path.join(DATA_DIR, "_apps")
SHARED_MACOS_APP = os.path.join(SHARED_APPS_DIR, "macOS", "Telegram.app")

# Original system proxy state, persisted before apply_proxy() changes it.
# Present on disk = a restore is pending (or failed); recovered at startup.
PROXY_ORIGINAL_FILE = os.path.join(DATA_DIR, "proxy_original.json")
_proxy_state_lock = threading.Lock()  # serializes check+write of the file above
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
    # Sanitise: bundle folder names may not contain /, be empty, or be "." / ".."
    safe_bundle_name = app_name.replace("/", "-").strip()
    if safe_bundle_name in ("", ".", ".."):
        safe_bundle_name = os.path.basename(account_path).replace("/", "-").strip() or "Telegram"
    app_dest = os.path.join(account_path, safe_bundle_name + ".app")
    r = subprocess.run(["cp", "-cR", shared_app_path, app_dest], capture_output=True)
    if r.returncode != 0:
        r2 = subprocess.run(["cp", "-R", shared_app_path, app_dest], capture_output=True)
        if r2.returncode != 0:
            return False
    # Strip Gatekeeper quarantine so macOS doesn't silently block launch
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", app_dest], capture_output=True)
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


def remove_cloned_app(account_path):
    if not get_shared_app():
        return
    app = find_account_app(account_path)
    if app and os.path.isdir(app):
        subprocess.run(["rm", "-rf", app], capture_output=True)


def clear_media_cache(account_path, threshold_mb=0):
    """Delete media_cache for account_path if it exceeds threshold_mb.

    threshold_mb=0 means always clear (no threshold check).
    Returns (cleared, freed_bytes) — cleared=False if cache was below threshold or missing.
    """
    cache_path = os.path.join(account_path, "TelegramForcePortable", "tdata", "media_cache")
    if not os.path.isdir(cache_path):
        return False, 0
    size = get_folder_size(cache_path)
    if threshold_mb > 0 and size < threshold_mb * 1024 * 1024:
        return False, 0
    subprocess.run(["rm", "-rf", cache_path], capture_output=True)
    _log.info("Cleared media_cache for %s (freed %s)", os.path.basename(account_path), human_size(size))
    return True, size


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
                tdata_size   = get_folder_size(tdata_actual) if tdata_actual else 0
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

def get_folder_size(path):
    total = 0
    try:
        for dp, _, fn in os.walk(path, followlinks=False):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total

def human_size(n):
    """Return a human-readable size string using decimal units (1 GB = 1,000,000,000 bytes),
    matching how macOS Finder displays file sizes."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            return f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"

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
    cache_total = sum(
        get_folder_size(os.path.join(a["path"], "TelegramForcePortable", "tdata", "media_cache"))
        for a in accs
        if os.path.isdir(os.path.join(a["path"], "TelegramForcePortable", "tdata", "media_cache"))
    )
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
                        subprocess.run(["rm", "-rf", app], capture_output=True)
                        invalidate_scan_cache()
                # Auto-clear media cache if over threshold
                if threshold_mb > 0:
                    clear_media_cache(p, threshold_mb)
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

def open_account(path):
    """Open the Telegram account at path. Returns (ok, message)."""
    _log.info("Opening account: %s", path)
    if is_running(path):
        _log.info("open_account: already running for %s", path)
        return True, "already running"
    app = find_account_app(path)
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

    return True, ""


def get_active_network_service():
    """Return the macOS network service name for the current default-route interface."""
    try:
        r = subprocess.run(["route", "get", "default"], capture_output=True, text=True, timeout=15)
        iface = ""
        for line in r.stdout.split("\n"):
            if "interface:" in line:
                iface = line.split(":")[-1].strip()
                break
        if not iface:
            return "Wi-Fi"
        r2 = subprocess.run(["networksetup", "-listnetworkserviceorder"],
                            capture_output=True, text=True, timeout=15)
        lines = r2.stdout.split("\n")
        for i, line in enumerate(lines):
            if f"Device: {iface}" in line:
                for j in range(i - 1, -1, -1):
                    if ")" in lines[j] and lines[j].strip():
                        return lines[j].split(")", 1)[-1].strip()
    except Exception:
        pass
    return "Wi-Fi"

def apply_proxy(proxy_config):
    """
    Temporarily set the macOS system SOCKS proxy so Telegram routes through it.
    Restores original proxy state after 35 seconds (enough for Telegram to connect).
    """

    proxy_type = proxy_config.get("type", "socks5").lower()
    host       = proxy_config.get("host", "")
    port       = str(proxy_config.get("port", 1080))
    if not host:
        return

    _log.info("Applying %s proxy %s:%s", proxy_type, host, port)
    service = get_active_network_service()

    def scutil_get_proxy():
        cmd = "socks" if proxy_type.startswith("socks") else "http"
        try:
            r = subprocess.run(["networksetup", f"-get{cmd}firewallproxy", service],
                               capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            _log.warning("scutil_get_proxy: networksetup query timed out")
            return False, "", "0"
        enabled = "Enabled: Yes" in r.stdout
        cur_host, cur_port = "", "0"
        for ln in r.stdout.split("\n"):
            if ln.startswith("Server:"): cur_host = ln.split(":", 1)[1].strip()
            if ln.startswith("Port:"):   cur_port = ln.split(":", 1)[1].strip()
        return enabled, cur_host, cur_port

    orig_enabled, orig_host, orig_port = scutil_get_proxy()

    # Persist the pre-change state so a failed restore can be recovered at
    # next startup. First writer wins: if a restore is already pending, the
    # file already holds the true original — do not overwrite it.
    #
    # The restore is then built from the FILE's baseline, not from this call's
    # live query: if two accounts on the same proxy channel are opened within
    # the 35 s window, the second's live query returns the FIRST account's
    # proxy (already applied), so baking that in used to restore the wrong
    # proxy and leave it stuck on. Reading back the authoritative baseline
    # makes every concurrent restore converge on the same true original.
    channel = "socks" if proxy_type.startswith("socks") else "http"
    r_enabled, r_host, r_port = orig_enabled, orig_host, orig_port
    with _proxy_state_lock:
        if not os.path.exists(PROXY_ORIGINAL_FILE):
            _save_json_atomic(PROXY_ORIGINAL_FILE, {
                "service": service, "proxy_type": proxy_type,
                "enabled": orig_enabled, "host": orig_host, "port": orig_port,
                "saved_at": datetime.now().isoformat(),
            })
        try:
            with open(PROXY_ORIGINAL_FILE) as _pf:
                _base = json.load(_pf)
            _base_channel = "socks" if str(_base.get("proxy_type", "")).startswith("socks") else "http"
            # Only trust the file when it describes THIS proxy channel; a
            # different channel is independent and its live query is correct.
            if _base_channel == channel:
                r_enabled = bool(_base.get("enabled"))
                r_host    = str(_base.get("host", "") or "")
                r_port    = str(_base.get("port", "0") or "0")
        except Exception as _e:
            _log.warning("apply_proxy: baseline read-back failed (%s); using live query", _e)

    # Build per-type command strings using shlex.quote() for all user-controlled values.
    # The initial set+enable pair runs via _run_as_admin() (temp-file approach — no AppleScript
    # string embedding).  Restore uses sudo -n with cached credentials via bash -c directly.
    if proxy_type.startswith("socks"):
        set_cmd     = f"networksetup -setsocksfirewallproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)} off"
        on_cmd      = f"networksetup -setsocksfirewallproxystate {shlex.quote(service)} on"
        off_cmd     = f"networksetup -setsocksfirewallproxystate {shlex.quote(service)} off"
        restore_set = f"networksetup -setsocksfirewallproxy {shlex.quote(service)} {shlex.quote(r_host)} {shlex.quote(r_port)} off"
    else:
        set_cmd     = f"networksetup -sethttpproxy {shlex.quote(service)} {shlex.quote(host)} {shlex.quote(port)}"
        on_cmd      = f"networksetup -sethttpproxystate {shlex.quote(service)} on"
        off_cmd     = f"networksetup -sethttpproxystate {shlex.quote(service)} off"
        restore_set = f"networksetup -sethttpproxy {shlex.quote(service)} {shlex.quote(r_host)} {shlex.quote(r_port)}"

    result = _run_as_admin(
        f"{set_cmd}\n{on_cmd}\n",
        "TelegramManager is setting a proxy for this Telegram account."
    )
    if result.returncode != 0:
        _log.warning("apply_proxy: admin prompt rejected or failed (rc=%d)", result.returncode)
        return

    _log.info("Proxy %s:%s active on service %r; will restore in 35 s", host, port, service)

    def schedule_restore():
        import tempfile, stat
        if r_enabled and r_host:
            restore_lines = [restore_set, on_cmd]
        else:
            restore_lines = [off_cmd]
        # Runs under `set -e`: only reached when the restore succeeded, so a
        # leftover file always means "system proxy may still be modified".
        restore_lines.append(f"rm -f {shlex.quote(PROXY_ORIGINAL_FILE)}")

        fd_restore, restore_path = tempfile.mkstemp(suffix=".sh", prefix="tm_proxy_restore_cmd_")
        fd_wrapper, wrapper_path = tempfile.mkstemp(suffix=".sh", prefix="tm_proxy_restore_job_")
        try:
            with os.fdopen(fd_restore, "w") as f:
                f.write("#!/bin/bash\n")
                f.write("set -e\n")
                f.write("\n".join(restore_lines) + "\n")
            os.chmod(restore_path, stat.S_IRWXU)

            prompt = _as_str("TelegramManager is restoring the proxy settings after Telegram connected.")
            admin_script = (
                f'do shell script "bash {shlex.quote(restore_path)}" '
                f'with administrator privileges with prompt {prompt}'
            )

            with os.fdopen(fd_wrapper, "w") as f:
                f.write("#!/bin/bash\n")
                f.write("sleep 35\n")
                f.write(f"osascript -e {shlex.quote(admin_script)} >/dev/null 2>&1 || true\n")
                f.write(f"rm -f {shlex.quote(restore_path)}\n")
                f.write(f"rm -f {shlex.quote(wrapper_path)}\n")
            os.chmod(wrapper_path, stat.S_IRWXU)

            subprocess.Popen(["/usr/bin/nohup", "bash", wrapper_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            _log.error("apply_proxy: failed to schedule restore for service %r: %s", service, e)
            try:
                os.unlink(restore_path)
            except OSError:
                pass
            try:
                os.unlink(wrapper_path)
            except OSError:
                pass

    schedule_restore()


def _recover_stale_proxy():
    """Restore system proxy settings left modified by a previous run.

    PROXY_ORIGINAL_FILE existing at startup means apply_proxy() changed the
    system proxy but its scheduled restore never completed (server killed,
    admin prompt denied, crash). Without this, the whole machine keeps
    routing through the last account's proxy indefinitely.
    """
    try:
        with open(PROXY_ORIGINAL_FILE) as f:
            rec = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        _log.error("stale proxy record unreadable (%s) — remove %s and check "
                   "System Settings → Network manually", e, PROXY_ORIGINAL_FILE)
        return

    service = rec.get("service", "")
    ptype   = str(rec.get("proxy_type", "socks5"))
    if not service:
        try:
            os.unlink(PROXY_ORIGINAL_FILE)
        except OSError:
            pass
        return

    _log.warning("Previous proxy restore did not complete — restoring original "
                 "settings for service %r", service)
    q = shlex.quote
    if ptype.startswith("socks"):
        if rec.get("enabled") and rec.get("host"):
            lines = [
                f"networksetup -setsocksfirewallproxy {q(service)} {q(str(rec.get('host')))} {q(str(rec.get('port', '0')))} off",
                f"networksetup -setsocksfirewallproxystate {q(service)} on",
            ]
        else:
            lines = [f"networksetup -setsocksfirewallproxystate {q(service)} off"]
    else:
        if rec.get("enabled") and rec.get("host"):
            lines = [
                f"networksetup -sethttpproxy {q(service)} {q(str(rec.get('host')))} {q(str(rec.get('port', '0')))}",
                f"networksetup -sethttpproxystate {q(service)} on",
            ]
        else:
            lines = [f"networksetup -sethttpproxystate {q(service)} off"]

    r = _run_as_admin(
        "\n".join(lines) + "\n",
        "TelegramManager needs to restore proxy settings left over from a previous run.",
    )
    if r.returncode == 0:
        try:
            os.unlink(PROXY_ORIGINAL_FILE)
        except OSError:
            pass
        _log.info("Stale proxy state restored for service %r", service)
    else:
        _log.error("Stale proxy restore failed (rc=%d) — the system proxy may "
                   "still be set; check System Settings → Network → %s",
                   r.returncode, service)


def list_backups():
    """Return all tdata backups found in ROOT_DIR/Backups/, newest first."""
    backup_root = os.path.join(DATA_DIR, "Backups")
    backups = []
    if not os.path.isdir(backup_root):
        return []
    for date_folder in sorted(os.listdir(backup_root), reverse=True):
        date_path = os.path.join(backup_root, date_folder)
        if not os.path.isdir(date_path):
            continue
        for account in sorted(os.listdir(date_path)):
            acc_path  = os.path.join(date_path, account)
            tdata_src = os.path.join(acc_path, "tdata")
            if os.path.isdir(tdata_src):
                size = get_folder_size(tdata_src)
                backups.append({
                    "date":       date_folder,
                    "account":    account,
                    "backup_path": acc_path,
                    "size":       size,
                    "size_human": human_size(size),
                })
    return backups


_backup_map_cache = {"ts": 0.0, "map": {}}

def _last_backup_map():
    """Map account name → newest backup date folder ("YYYY-MM-DD_HH-MM").

    listdir-only (no sizes), cached 30 s — cheap enough for the accounts poll.
    """
    now = time.time()
    if now - _backup_map_cache["ts"] < 30:
        return _backup_map_cache["map"]
    backup_root = os.path.join(DATA_DIR, "Backups")
    result = {}
    if os.path.isdir(backup_root):
        try:
            for date_folder in sorted(os.listdir(backup_root), reverse=True):
                date_path = os.path.join(backup_root, date_folder)
                if not os.path.isdir(date_path):
                    continue
                for account in os.listdir(date_path):
                    result.setdefault(account, date_folder)
        except OSError as e:
            _log.warning("_last_backup_map: %s", e)
    _backup_map_cache["map"] = result
    _backup_map_cache["ts"] = now
    return result


def delete_backup(backup_path):
    """Delete one backup folder (Backups/<date>/<account>). Returns (ok, msg).

    Only accepts paths exactly two levels below DATA_DIR/Backups so a crafted
    request can never delete the whole Backups tree or anything outside it.
    """
    backup_root = os.path.realpath(os.path.join(DATA_DIR, "Backups"))
    real = os.path.realpath(str(backup_path or ""))
    rel = os.path.relpath(real, backup_root)
    if rel.startswith("..") or len(rel.split(os.sep)) != 2:
        return False, "Invalid backup path"
    if not os.path.isdir(real):
        return False, "Backup not found"
    r = subprocess.run(["rm", "-rf", real], capture_output=True, timeout=120)
    if r.returncode != 0:
        return False, "Delete failed: " + r.stderr.decode(errors="replace").strip()[:200]
    # drop the date folder too once its last account backup is gone
    try:
        os.rmdir(os.path.dirname(real))
    except OSError:
        pass
    _log.info("Backup deleted: %s", real)
    _backup_map_cache["ts"] = 0.0
    return True, "Backup deleted"


def prune_backups(account_name):
    """Enforce backup_keep_per_account for one account (0 = keep all)."""
    try:
        keep = int(config.get("backup_keep_per_account", 0))
    except (TypeError, ValueError):
        keep = 0
    if keep <= 0:
        return
    mine = [b for b in list_backups() if b["account"] == account_name]  # newest first
    for b in mine[keep:]:
        ok, msg = delete_backup(b["backup_path"])
        if ok:
            _log.info("prune_backups: removed old backup %s", b["backup_path"])
        else:
            _log.warning("prune_backups: could not remove %s: %s", b["backup_path"], msg)


def restore_backup(backup_path, account_path):
    """Copy tdata from a backup folder back into the account's TelegramForcePortable/."""
    _log.info("Restoring backup from %s into %s", backup_path, account_path)
    tdata_src = os.path.join(backup_path, "tdata")
    portable  = os.path.join(account_path, "TelegramForcePortable")
    tdata_dst = os.path.join(portable, "tdata")

    if not os.path.isdir(tdata_src):
        _log.warning("restore_backup: tdata not found at %s", tdata_src)
        return False, "Backup tdata not found"
    if not os.path.isdir(account_path):
        _log.warning("restore_backup: account folder not found: %s", account_path)
        return False, "Account folder not found"
    if find_telegram_pid(account_path):
        _log.warning("restore_backup: refused — Telegram is running for %s", account_path)
        return False, "Close Telegram for this account before restoring a backup"

    os.makedirs(portable, exist_ok=True)

    # Move current tdata to a timestamped .bak before overwriting
    bak = None
    if os.path.isdir(tdata_dst):
        bak = tdata_dst + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        os.rename(tdata_dst, bak)
        _log.info("Existing tdata moved to %s", bak)

    r = subprocess.run(["cp", "-R", tdata_src, tdata_dst], capture_output=True)
    if r.returncode != 0:
        _log.error("restore_backup: cp failed: %s", r.stderr.decode(errors="replace").strip())
        # Roll back: restore the original tdata from the .bak
        if bak and os.path.isdir(bak):
            try:
                os.rename(bak, tdata_dst)
                _log.info("restore_backup: original tdata restored from %s", bak)
            except Exception as undo_e:
                _log.error("restore_backup: rollback also failed: %s", undo_e)
        return False, "Copy failed — original tdata has been restored"

    _log.info("Backup restored successfully")
    return True, "Restored successfully. The previous tdata was kept as a .bak folder."


# ── Workspaces ────────────────────────────────────────────────────────────

def load_workspaces():
    data, source = _load_json_file_with_fallbacks(WORKSPACES_FILE, {}, (LEGACY_WORKSPACES_FILE,))
    if source == LEGACY_WORKSPACES_FILE and WORKSPACES_FILE != LEGACY_WORKSPACES_FILE:
        try:
            _save_json_atomic(WORKSPACES_FILE, data)
            os.unlink(LEGACY_WORKSPACES_FILE)
        except OSError:
            pass
    return data

def save_workspaces(ws):
    """Atomic write: write to .tmp then rename, so a crash can't corrupt the file."""
    with _ws_lock:
        _save_json_atomic(WORKSPACES_FILE, ws)


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
        "last_opened": {}, "pinned": [], "proxies": {}, "dock_names": {}
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

def kill_account(account_path):
    """Kill only the Telegram process for this account."""
    pid = find_telegram_pid(account_path)
    if pid:
        _log.info("Killing Telegram PID %d for account %s", pid, account_path)
        subprocess.run(["kill", str(pid)], capture_output=True)
        def _cleanup(p):
            time.sleep(2)
            remove_cloned_app(p)
            invalidate_scan_cache()
        threading.Thread(target=_cleanup, args=(account_path,), daemon=True).start()
        return True
    return False




# ── Session Keeper ─────────────────────────────────────────────────────────

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

        if config.get("keeper_enabled", False):
            if _keeper_lock.acquire(blocking=False):
                try:
                    interval_days = config.get("keeper_interval_days", 30)
                    open_secs     = config.get("keeper_open_seconds", 120)

                    _keeper_status["running"]  = True
                    _keeper_status["last_run"] = datetime.now().isoformat()

                    _run_keeper_pass(interval_days, open_secs)
                finally:
                    _keeper_status["running"] = False
                    _keeper_lock.release()
            else:
                _log.info("run_keeper_loop: keeper already running — skipping this cycle")

        # Sleep at the END so the first check runs immediately on startup.
        time.sleep(CHECK_INTERVAL)


def _run_keeper_pass(interval_days, open_secs):
    """Open every account that hasn't been seen in interval_days. Shared by the
    scheduled loop and the manual 'Run Now' trigger."""
    for acc in scan_accounts():
        if acc["status"] != "ready":
            continue
        if acc["running"]:
            # Already open — counts as a keepalive; update last_opened
            with _meta_lock:
                metadata.setdefault("last_opened", {})[acc["path"]] = datetime.now().isoformat()
                save_metadata(metadata)
            continue

        with _meta_lock:
            last_iso = metadata.get("last_opened", {}).get(acc["path"])
        days_since = 999
        if last_iso:
            try:
                days_since = (time.time() - datetime.fromisoformat(last_iso).timestamp()) / 86400
            except Exception:
                pass

        if days_since >= interval_days:
            _keeper_status["last_account"] = acc["name"]
            ok, msg = open_account(acc["path"])
            if not ok:
                _log.error("Keeper: failed to open %s: %s", acc["name"], msg)
            time.sleep(open_secs)
            kill_account(acc["path"])
            time.sleep(5)   # brief pause between accounts


def trigger_keeper_now():
    """Force an immediate keeper run in a background thread.

    Returns (started, message). If a keeper pass is already in progress the
    request is rejected rather than running a second concurrent pass.
    """
    if not _keeper_lock.acquire(blocking=False):
        _log.info("trigger_keeper_now: keeper already running — request ignored")
        return False, "keeper already running"

    def run_once():
        try:
            _keeper_status["running"]  = True
            _keeper_status["last_run"] = datetime.now().isoformat()
            _run_keeper_pass(
                config.get("keeper_interval_days", 30),
                config.get("keeper_open_seconds", 120),
            )
        finally:
            _keeper_status["running"] = False
            _keeper_lock.release()

    threading.Thread(target=run_once, daemon=True).start()
    return True, "Keeper started"


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
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run([pb, "-c", f"Add :{key} string {safe_dn}", plist],
                           capture_output=True)
    # Re-sign with ad-hoc signature (required after modifying Info.plist)
    r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app_path],
                       capture_output=True)
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
                    "last_opened", "proxies", "dock_names"):
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

    safe_bundle = name.replace("/", "-").strip()
    if safe_bundle in ("", ".", ".."):
        safe_bundle = os.path.basename(folder_path) or "Telegram"
    app_dest = os.path.join(folder_path, safe_bundle + ".app")
    # Use APFS clone (cp -cR) when copying from the shared master — saves ~300 MB per account
    r = subprocess.run(["cp", "-cR", app_source, app_dest], capture_output=True)
    if r.returncode != 0:
        r = subprocess.run(["cp", "-R", app_source, app_dest], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["rm", "-rf", folder_path], capture_output=True)
        return False, f"Failed to copy Telegram.app: {r.stderr.decode(errors='replace').strip()}"

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
        safe_name = raw_name.replace("/", "-").strip()
        if safe_name in ("", ".", ".."):
            safe_name = os.path.basename(folder_path) or "Telegram"
        app_dest = os.path.join(folder_path, safe_name + ".app")
        r = subprocess.run(["cp", "-cR", app_source, app_dest], capture_output=True)
        if r.returncode != 0:
            r = subprocess.run(["cp", "-R", app_source, app_dest], capture_output=True)
        if r.returncode != 0:
            return False, f"Failed to copy Telegram.app: {r.stderr.decode(errors='replace').strip()}"

    os.makedirs(portable, exist_ok=True)

    if os.path.isdir(tdata_src) and not os.path.isdir(tdata_dest):
        r = subprocess.run(["mv", tdata_src, tdata_dest], capture_output=True)
        if r.returncode != 0:
            return False, f"Failed to move tdata: {r.stderr.decode(errors='replace').strip()}"

    for f in ("Telegram.exe", "Updater.exe", "log.txt", "log_start0.txt"):
        fp = os.path.join(folder_path, f)
        if os.path.exists(fp): os.remove(fp)
    modules = os.path.join(folder_path, "modules")
    if os.path.isdir(modules):
        subprocess.run(["rm", "-rf", modules])

    # Set the Dock name to the account folder name
    account_name = os.path.basename(folder_path)
    set_telegram_display_name(folder_path, account_name)

    return True, "Setup complete"

def backup_account(folder_path, account_name):
    _log.info("Backing up account %r from %s", account_name, folder_path)
    date_str   = datetime.now().strftime("%Y-%m-%d_%H-%M")
    backup_dir = os.path.join(DATA_DIR, "Backups", date_str, account_name)
    tdata_src  = os.path.join(folder_path, "TelegramForcePortable", "tdata")
    if not os.path.isdir(tdata_src):
        _log.warning("backup_account: tdata not found at %s", tdata_src)
        return False, "No tdata found", ""
    # Refuse to backup while Telegram is writing to tdata — the copy would be inconsistent
    if find_telegram_pid(folder_path):
        _log.warning("backup_account: refused — Telegram is running for %s", folder_path)
        return False, "Close Telegram for this account before backing up", ""
    os.makedirs(backup_dir, exist_ok=True)
    r = subprocess.run(["cp", "-R", tdata_src, os.path.join(backup_dir, "tdata")],
                       capture_output=True)
    if r.returncode != 0:
        return False, f"Copy failed: {r.stderr.decode(errors='replace').strip()}", ""
    _log.info("Backup complete: %s", backup_dir)
    prune_backups(account_name)
    _backup_map_cache["ts"] = 0.0
    return True, f"Backed up to Backups/{date_str}/{account_name}", backup_dir

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
        latest_mod, latest = 0, None
        for acc in scan_accounts():
            ap = find_account_app(acc["path"])
            if ap:
                mod = os.path.getmtime(ap)
                if mod > latest_mod:
                    latest_mod, latest = mod, ap
        if latest:
            app_source = latest

    shared = get_shared_app()
    if shared:
        if not os.path.isdir(app_source):
            return False, "Set Telegram.app source path in Settings first"
        shared_tmp = shared + ".new"
        subprocess.run(["rm", "-rf", shared_tmp], capture_output=True)
        r = subprocess.run(["cp", "-R", app_source, shared_tmp], capture_output=True)
        if r.returncode != 0:
            subprocess.run(["rm", "-rf", shared_tmp], capture_output=True)
            return False, "Failed to update shared Telegram.app"
        subprocess.run(["rm", "-rf", shared], capture_output=True)
        os.rename(shared_tmp, shared)
        return True, "Shared Telegram.app updated"

    if not os.path.isdir(app_source):
        latest_mod, latest = 0, None
        for acc in scan_accounts():
            ap = find_account_app(acc["path"])
            if ap:
                mod = os.path.getmtime(ap)
                if mod > latest_mod:
                    latest_mod, latest = mod, ap
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
            subprocess.run(["rm", "-rf", app_tmp], capture_output=True)
            r = subprocess.run(["cp", "-R", app_source, app_tmp], capture_output=True)
            if r.returncode != 0:
                subprocess.run(["rm", "-rf", app_tmp], capture_output=True)
                _log.warning("update_all_apps: cp failed for %s — skipping", acc["path"])
                continue
            subprocess.run(["rm", "-rf", app_dest], capture_output=True)
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
            subprocess.run(["kill", "-9", str(pid)], capture_output=True)
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
        cleared = []
        errors  = []
        targets = ["settings0", "settings1"]
        for fname in targets:
            fpath = os.path.join(tdata, fname)
            if os.path.exists(fpath):
                try:
                    if os.path.isdir(fpath):
                        subprocess.run(["rm", "-rf", fpath], capture_output=True)
                    else:
                        os.remove(fpath)
                    cleared.append(fname)
                except Exception as e:
                    errors.append(f"{fname}: {e}")
        # Also clear media_cache if it exists
        mc = os.path.join(tdata, "media_cache")
        if os.path.isdir(mc):
            r = subprocess.run(["rm", "-rf", mc], capture_output=True)
            if r.returncode == 0:
                cleared.append("media_cache")
            else:
                errors.append(f"media_cache: rm failed")
        if errors:
            results.append({"action": "clear_cache", "ok": False,
                            "msg": f"Cleared: {', '.join(cleared)}. Errors: {'; '.join(errors)}"})
        else:
            results.append({"action": "clear_cache", "ok": True,
                            "msg": f"Cleared: {', '.join(cleared) if cleared else 'nothing to clear'}"})

    if "fix_perms" in actions:
        binary = os.path.join(app, "Contents", "MacOS", "Telegram")
        if os.path.exists(binary):
            try:
                os.chmod(binary, 0o755)
                # Also re-sign after permission fix
                r = subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app],
                                   capture_output=True)
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
                subprocess.run(["rm", "-rf", app], capture_output=True)
            r = subprocess.run(["cp", "-R", app_source, app], capture_output=True)
            if r.returncode == 0:
                # Re-apply dock name
                account_name = os.path.basename(account_path)
                set_telegram_display_name(account_path, account_name)
                results.append({"action": "recopy_app", "ok": True,
                                "msg": "Telegram.app replaced with a fresh copy"})
            else:
                results.append({"action": "recopy_app", "ok": False,
                                "msg": r.stderr.decode(errors="replace").strip() or "cp failed"})

    return results


class RequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = _route_path(self.path)
        if path is None:
            self.send_response(404)
            self.end_headers()
            return
        if path == "/api/accounts":
            accs = scan_accounts_cached()
            last_map = _last_backup_map()
            for a in accs:
                a["last_backup"] = last_map.get(a["name"], "")
            self.send_json(accs)
        elif path == "/api/config":
            self.send_json({**config, "root_dir": ROOT_DIR})
        elif path == "/api/backups":
            self.send_json(list_backups())
        elif path == "/api/workspaces":
            self.send_json(load_workspaces())
        elif path == "/api/keeper/status":
            self.send_json({**_keeper_status,
                            "enabled":       config.get("keeper_enabled", False),
                            "interval_days": config.get("keeper_interval_days", 30),
                            "open_seconds":  config.get("keeper_open_seconds", 120)})
        elif path == "/api/alerts":
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
        elif path == "/api/stats":
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
        elif path == "/api/groups":
            self.send_json(list_groups())

        elif path == "/api/pick-app":
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

        elif path == "/api/shared-app/status":
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
                setup_source = ""
                setup_source_type = "none"
                for acc in accs:
                    ap = find_account_app(acc["path"])
                    if ap:
                        setup_source = ap
                        setup_source_type = "account"
                        break
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

        elif path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if origin and not (origin.startswith("http://127.0.0.1:") or
                           origin.startswith("http://localhost:")):
            self.send_json({"success": False, "message": "Forbidden"}, 403)
            return
        path   = _route_path(self.path)
        if path is None:
            self.send_json({"success": False, "message": "Not found"}, 404)
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

        if path == "/api/open":
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

        elif path == "/api/open-all":
            to_open = [acc for acc in scan_accounts()
                       if acc["status"] == "ready" and not acc["running"]]
            def _open_all(accounts_to_open):
                for acc in accounts_to_open:
                    ok, msg = open_account(acc["path"])
                    if not ok:
                        _log.error("open-all: failed to open %s: %s", acc["name"], msg)
                    time.sleep(0.5)
                invalidate_scan_cache()
            threading.Thread(target=_open_all, args=(to_open,), daemon=True).start()
            self.send_json({"success": True, "opened": len(to_open)})

        elif path == "/api/open-group":
            group   = data.get("group", "")
            to_open = [acc for acc in scan_accounts()
                       if acc["group"] == group and acc["status"] == "ready" and not acc["running"]]
            def _open_group(accounts_to_open):
                for acc in accounts_to_open:
                    ok, msg = open_account(acc["path"])
                    if not ok:
                        _log.error("open-group: failed to open %s: %s", acc["name"], msg)
                    time.sleep(0.5)
                invalidate_scan_cache()
            threading.Thread(target=_open_group, args=(to_open,), daemon=True).start()
            self.send_json({"success": True, "opened": len(to_open)})

        elif path == "/api/open-pinned":
            to_open = [acc for acc in scan_accounts()
                       if acc["pinned"] and acc["status"] == "ready" and not acc["running"]]
            def _open_pinned(accounts_to_open):
                for acc in accounts_to_open:
                    ok, msg = open_account(acc["path"])
                    if not ok:
                        _log.error("open-pinned: failed to open %s: %s", acc["name"], msg)
                    time.sleep(0.5)
                invalidate_scan_cache()
            threading.Thread(target=_open_pinned, args=(to_open,), daemon=True).start()
            self.send_json({"success": True, "opened": len(to_open)})

        elif path == "/api/close-all":
            close_all()
            self.send_json({"success": True})

        elif path == "/api/close-account":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok = kill_account(acc_path)
            if ok:
                self.send_json({"success": True})
            else:
                self.send_json({"success": False, "message": "Account is not running"})

        elif path == "/api/setup":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg = setup_account(acc_path)
            self.send_json({"success": ok, "message": msg})

        elif path == "/api/setup-all":
            to_setup = [acc for acc in scan_accounts() if acc["status"] == "needs_setup"]
            def _setup_all(accounts_to_setup):
                for acc in accounts_to_setup:
                    setup_account(acc["path"])
                invalidate_scan_cache()
            threading.Thread(target=_setup_all, args=(to_setup,), daemon=True).start()
            self.send_json({"success": True, "count": len(to_setup)})

        elif path == "/api/backup":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg, backup_path = backup_account(acc_path, data.get("name", "account"))
            self.send_json({"success": ok, "message": msg, "backup_path": backup_path})

        elif path == "/api/backup/delete":
            ok, msg = delete_backup(data.get("backup_path", ""))
            if ok:
                invalidate_scan_cache()
            self.send_json({"success": ok, "message": msg})

        elif path == "/api/backup-all":
            to_backup = [acc for acc in scan_accounts() if acc["status"] == "ready"]
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

        elif path == "/api/update-all":
            ok, msg = update_all_apps()
            self.send_json({"success": ok, "message": msg})

        elif path == "/api/note":
            acc_path = data.get("path", "")
            note     = data.get("note", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"}); return
            with _meta_lock:
                metadata.setdefault("notes", {})[acc_path] = note
                save_metadata(metadata)
            self.send_json({"success": True})

        elif path == "/api/reorder":
            orders = data.get("orders", {})
            if not isinstance(orders, dict) or not all(is_safe_path(k) for k in orders):
                self.send_json({"success": False, "message": "Invalid path in order"}); return
            with _meta_lock:
                metadata.setdefault("order", {}).update(orders)
                save_metadata(metadata)
            self.send_json({"success": True})

        elif path == "/api/color":
            acc_path = data.get("path", "")
            color    = data.get("color", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"}); return
            with _meta_lock:
                metadata.setdefault("colors", {})[acc_path] = color
                save_metadata(metadata)
            self.send_json({"success": True})

        elif path == "/api/username":
            acc_path = data.get("path", "")
            username = data.get("username", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"}); return
            with _meta_lock:
                metadata.setdefault("usernames", {})[acc_path] = username
                save_metadata(metadata)
            self.send_json({"success": True})

        elif path == "/api/rename":
            old_path = data.get("path", "")
            new_name = data.get("new_name", "").strip()
            if not is_safe_path(old_path):
                self.send_json({"success": False, "message": "Invalid path"})
            elif not new_name or "/" in new_name or new_name in (".", "..") or new_name in SKIP_NAMES:
                self.send_json({"success": False, "message": "Invalid name"})
            else:
                ok, result = rename_account(old_path, new_name)
                if ok:
                    self.send_json({"success": True, "new_path": result})
                else:
                    self.send_json({"success": False, "message": result})

        elif path == "/api/pin":
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

        elif path == "/api/proxy":
            acc_path = data.get("path", "")
            proxy    = data.get("proxy")   # dict with type/host/port/user/pass, or null to clear
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"}); return
            with _meta_lock:
                if proxy and proxy.get("host"):
                    metadata.setdefault("proxies", {})[acc_path] = proxy
                else:
                    metadata.get("proxies", {}).pop(acc_path, None)
                save_metadata(metadata)
            self.send_json({"success": True})

        elif path == "/api/dock-name":
            acc_path  = data.get("path", "")
            dock_name = data.get("dock_name", "").strip()
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
                safe_bundle = effective_name.replace("/", "-").strip() or effective_name
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

        elif path == "/api/restore":
            backup_path  = data.get("backup_path", "")
            account_path = data.get("account_path", "")
            if not is_safe_path(backup_path) or not is_safe_path(account_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg = restore_backup(backup_path, account_path)
            self.send_json({"success": ok, "message": msg})

        elif path == "/api/workspace/save":
            name          = data.get("name", "").strip()
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

        elif path == "/api/workspace/open":
            name = data.get("name", "")
            ws   = load_workspaces()
            if name not in ws:
                self.send_json({"success": False, "message": "Workspace not found"})
            else:
                accs    = {a["path"]: a for a in scan_accounts()}
                to_open = [accs[p] for p in ws[name].get("accounts", [])
                           if p in accs and accs[p]["status"] == "ready"
                           and not accs[p]["running"]]
                def _open_workspace(accounts_to_open):
                    for acc in accounts_to_open:
                        ok, msg = open_account(acc["path"])
                        if not ok:
                            _log.error("open-workspace: failed to open %s: %s", acc["name"], msg)
                        time.sleep(0.5)
                    invalidate_scan_cache()
                threading.Thread(target=_open_workspace, args=(to_open,), daemon=True).start()
                self.send_json({"success": True, "opened": len(to_open)})

        elif path == "/api/workspace/delete":
            name = data.get("name", "")
            with _ws_lock:
                ws = load_workspaces()
                ws.pop(name, None)
                save_workspaces(ws)
            self.send_json({"success": True})

        elif path == "/api/keeper/run-now":
            started, msg = trigger_keeper_now()
            self.send_json({"success": started, "message": msg})

        elif path == "/api/fix-dock-names":
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

        elif path == "/api/reveal":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            if not os.path.isdir(acc_path):
                self.send_json({"success": False, "message": "Folder not found"})
                return
            subprocess.Popen(["open", "-R", acc_path])
            self.send_json({"success": True})

        elif path == "/api/delete":
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
                                            "proxies"):
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

        elif path == "/api/create-account":
            name        = data.get("name", "").strip()
            parent_path = data.get("parent_path", ROOT_DIR)
            open_after  = data.get("open_after", True)
            if parent_path and not is_safe_path(parent_path):
                self.send_json({"success": False, "message": "Invalid path"}); return
            ok, result  = create_account(name, parent_path, open_after)
            if ok:
                self.send_json({"success": True, "path": result})
            else:
                self.send_json({"success": False, "message": result})

        elif path == "/api/diagnose":
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

        elif path == "/api/repair":
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

        elif path == "/api/config":
            int_keys  = ("keeper_interval_days", "keeper_open_seconds",
                         "auto_clear_cache_mb", "backup_keep_per_account",
                         "lock_timeout_minutes")
            bool_keys = ("keeper_enabled", "proxy_system_apply")
            with _config_lock:
                for k in ("app_source", "extra_scan_dirs",
                          "keeper_enabled", "keeper_interval_days", "keeper_open_seconds",
                          "auto_clear_cache_mb", "proxy_system_apply",
                          "backup_keep_per_account",
                          "lock_password_hash", "lock_password_salt", "lock_hint",
                          "lock_timeout_minutes"):
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
                            continue
                    elif k == "app_source" and v and not is_allowed_app_source(v):
                        # app_source is later copied + quarantine-stripped + launched,
                        # so a token-only caller must not be able to set it to an
                        # arbitrary path. Empty = clear (falls back to auto-detect).
                        _log.warning("config: rejecting untrusted app_source=%r", v)
                        continue
                    config[k] = v
                save_config(config)
            invalidate_scan_cache()
            # Validate extra_scan_dirs and report any that don't exist
            bad_dirs = [
                d for d in config.get("extra_scan_dirs", [])
                if d and not os.path.isdir(os.path.expanduser(d))
            ]
            self.send_json({"success": True, "bad_dirs": bad_dirs})


        elif path == "/api/shared-app/setup":
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
                    # Fall back: find newest per-account copy
                    latest_mod, latest = 0, None
                    for acc in scan_accounts():
                        ap = find_account_app(acc["path"])
                        if ap:
                            mod = os.path.getmtime(ap)
                            if mod > latest_mod:
                                latest_mod, latest = mod, ap
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
            shared_dir = os.path.join(SHARED_APPS_DIR, "macOS")
            os.makedirs(shared_dir, exist_ok=True)
            dest = os.path.join(shared_dir, "Telegram.app")
            if os.path.isdir(dest):
                subprocess.run(["rm", "-rf", dest], capture_output=True)
            r = subprocess.run(["cp", "-R", app_source, dest], capture_output=True)
            if r.returncode != 0:
                self.send_json({"success": False, "message": "Copy failed: " + r.stderr.decode()})
                return
            # Strip quarantine so the master (and every clone from it) launches without Gatekeeper prompts
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", dest], capture_output=True)
            invalidate_scan_cache()
            self.send_json({"success": True,
                            "message": "Shared Telegram.app is ready. Accounts will use it on next open."})

        elif path == "/api/shared-app/remove-account-app":
            acc_path = data.get("path", "")
            if not acc_path or not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            app_path = find_account_app(acc_path)
            if not app_path:
                self.send_json({"success": False, "message": "No Telegram app bundle found in this folder"})
                return
            sz = get_folder_size(app_path)
            subprocess.run(["rm", "-rf", app_path], capture_output=True)
            invalidate_scan_cache()
            self.send_json({"success": True, "freed": sz, "freed_human": human_size(sz)})

        elif path == "/api/shared-app/remove-all-account-apps":
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
                    subprocess.run(["rm", "-rf", app_path], capture_output=True)
                    removed += 1
            invalidate_scan_cache()
            self.send_json({"success": True, "removed": removed,
                            "freed": freed, "freed_human": human_size(freed)})

        elif path == "/api/export-config":
            self.send_json({
                "version":    1,
                "exported_at": datetime.now().isoformat(),
                "metadata":   load_metadata(),
                "config":     load_config(),
                "workspaces": load_workspaces(),
            })

        elif path == "/api/import-config":
            ok, message, normalized = _validate_import_payload(data)
            if not ok:
                self.send_json({"success": False, "message": message})
                return
            imported_cfg = normalized["config"]
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

        else:
            self.send_json({"success": False, "message": "Unknown endpoint"}, 404)

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
    server = ThreadedHTTPServer(("127.0.0.1", port), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _log.info("Server shutting down")
