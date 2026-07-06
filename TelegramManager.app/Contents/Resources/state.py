"""Shared state singletons: config/metadata/workspaces persistence, path
resolution, and pure stdlib helpers used across server.py/proxy.py/backups.py/
keeper.py.

This module has ZERO repo-internal imports — it is the single owner of the
mutable shared dicts (config, metadata) and file-backed state, so nothing can
form an import cycle through it.
"""

import copy
import functools
import json
import logging
import os
import shlex
import subprocess
import threading
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
# A missing TG/ or data/ silently rescans the wrong tree and resets config
# (including the lock password) — collect warnings to log and show in the UI.
PATH_WARNINGS = []
if ROOT_DIR == _PARENT_DIR:
    PATH_WARNINGS.append(f"TG/ folder not found — scanning the whole parent dir instead: {_PARENT_DIR}")
if DATA_DIR == _PARENT_DIR:
    PATH_WARNINGS.append(f"data/ folder not found — config/backups fall back to: {_PARENT_DIR}")
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
    """Return True if path resolves to strictly within ROOT_DIR or DATA_DIR
    (prevents path traversal). ROOT_DIR/DATA_DIR themselves are rejected —
    every caller expects an account/backup subpath, never the root itself,
    so accepting the root would let a client operate on the whole managed
    tree (delete, rename, clear-cache, ...) instead of one account."""
    if not path:
        return False
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        for base in (ROOT_DIR, DATA_DIR):
            real_base = os.path.realpath(base)
            if real_path.startswith(real_base + os.sep):
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
    # cfg holds lock_password_hash/lock_password_salt — route through
    # _save_json_atomic so the file gets the same 0600 chmod as metadata,
    # instead of inheriting the process umask (commonly world-readable).
    with _config_lock:
        _save_json_atomic(CONFIG_FILE, cfg)
    # Mirror port to true parent dir so the Swift launcher always finds it (atomic write)
    if DATA_DIR != _PARENT_DIR:
        try:
            mirror = os.path.join(_PARENT_DIR, "manager_config.json")
            mirror_tmp = mirror + ".tmp"
            with open(mirror_tmp, "w") as f:
                json.dump({"port": cfg.get("port", 8477)}, f)
            os.replace(mirror_tmp, mirror)
        except OSError as e:
            # The Swift launcher reads this mirror to find the port — a failed
            # write means the next launch may connect to the wrong port.
            _log.warning("save_config: could not mirror port to %s: %s", _PARENT_DIR, e)

def load_metadata():
    data, source = _load_json_file_with_fallbacks(
        METADATA_FILE,
        {"notes": {}, "usernames": {}, "order": {}, "colors": {},
         "last_opened": {}, "pinned": [], "proxies": {}, "avatars": {}},
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

# One lock per account path, so open/backup/restore for the SAME account can't
# interleave (double-launch against one tdata, torn backup, tdata-less restore).
# Different accounts stay fully parallel. kill/close intentionally stays
# lock-free so an account can always be force-closed, even mid-backup.
_account_op_locks = {}
_account_op_locks_guard = threading.Lock()

def _account_path_lock(path):
    key = os.path.realpath(path)
    with _account_op_locks_guard:
        lk = _account_op_locks.get(key)
        if lk is None:
            lk = _account_op_locks[key] = threading.Lock()
    return lk

def serialize_account_op(get_path, busy_result):
    """Decorate an account operation so it holds that account's lock for its
    whole run. get_path(*args) picks the account path from the call; a caller
    that can't get the lock immediately gets busy_result (shaped to match the
    function's normal return) instead of blocking."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            lk = _account_path_lock(get_path(*a, **k))
            if not lk.acquire(blocking=False):
                _log.info("serialize_account_op: %s busy for %s", fn.__name__, get_path(*a, **k))
                return busy_result
            try:
                return fn(*a, **k)
            finally:
                lk.release()
        return wrapper
    return deco

_BUSY_MSG = "Another operation is already in progress for this account — try again in a moment."

def save_metadata(meta):
    """Atomic write: write to .tmp then rename, so a crash can't corrupt the file."""
    with _meta_lock:
        _save_json_atomic(METADATA_FILE, meta)


config   = load_config()
metadata = load_metadata()

# Original system proxy state, persisted before apply_proxy() changes it.
# Present on disk = a restore is pending (or failed); recovered at startup.
PROXY_ORIGINAL_FILE = os.path.join(DATA_DIR, "proxy_original.json")
_proxy_state_lock = threading.Lock()  # serializes check+write of the file above

# Regenerable cache directory names found INSIDE tdata/user_data* (media/file
# caches plus the Chromium caches of Telegram's embedded bot/mini-app WebView).
# None of these hold session/login data — Telegram rebuilds them on demand.
# Session data lives in key_datas, settings*, and the hex account dirs, which we
# never touch: cache clearing only ever descends into user_data* subtrees.
CACHE_DIR_NAMES = {
    "media_cache", "cache", "Cache", "Cache_Data", "GPUCache", "Code Cache",
    "ShaderCache", "GrShaderCache", "GraphiteDawnCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "component_crx_cache",
}
# Top-level regenerable dirs (re-downloaded on next launch).
CACHE_TOPLEVEL_NAMES = ("emoji", "dumps")
# Basenames excluded from backups (superset — a backup only needs the session).
BACKUP_EXCLUDE_DIR_NAMES = sorted(CACHE_DIR_NAMES | set(CACHE_TOPLEVEL_NAMES))


def _find_cache_dirs(tdata):
    """Return the top-most cache directories under tdata, scoped to user_data*
    subtrees and the known top-level cache dirs — never the session dirs."""
    targets = []
    for name in CACHE_TOPLEVEL_NAMES:
        p = os.path.join(tdata, name)
        if os.path.isdir(p):
            targets.append(p)
    try:
        roots = [os.path.join(tdata, n) for n in os.listdir(tdata)
                 if n.startswith("user_data") and os.path.isdir(os.path.join(tdata, n))]
    except OSError:
        roots = []
    for root in roots:
        for dp, dirnames, _ in os.walk(root, topdown=True):
            for d in list(dirnames):
                if d in CACHE_DIR_NAMES:
                    targets.append(os.path.join(dp, d))
                    dirnames.remove(d)   # don't descend into a dir we'll delete
    return targets

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
