# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the App

There is no build step for Python files — they run directly. The only compile step is the Swift native window, handled automatically by `launcher.sh`.

**Full app (native WKWebView window):**
```bash
open TelegramManager.app
# or, to run the server standalone for testing:
python3 "TelegramManager.app/Contents/Resources/server.py"
```

**Compile the Swift launcher manually (auto-done by launcher.sh):**
```bash
swiftc "TelegramManager.app/Contents/Resources/launcher.swift" \
    -o "TelegramManager.app/Contents/MacOS/launcher_swift" \
    -framework Cocoa -framework WebKit -framework Foundation -O
```

**Tail logs:**
```bash
tail -f data/manager.log
```

**Run tests + syntax check (do both before committing):**
```bash
python3 -m unittest discover -s tests -q
python3 -m py_compile "TelegramManager.app/Contents/Resources/server.py"
```

## Directory Layout

```
TelegramManager_backup_v3/       ← PARENT_DIR
  TelegramManager.app/           ← full app bundle
    Contents/
      MacOS/
        launcher.sh              ← entry point: compiles Swift, falls back to Chrome
        launcher_swift           ← compiled Swift binary (gitignored artifact)
      Resources/
        server.py                ← backend (Python, ~3300 lines)
        app_window.py            ← PyObjC fallback window (unused when Swift binary exists)
        index.html               ← frontend (single-file, inline CSS+JS, ~3700 lines)
        launcher.swift           ← Swift WKWebView window source
  TG/                            ← ROOT_DIR — account folders live here
  data/                          ← DATA_DIR — config, logs, backups, _apps/
    manager_config.json          ← runtime config (port, feature flags, scan dirs)
    Telegram.app                 ← shared Telegram master (zero-config placement, optional)
    _apps/macOS/Telegram.app     ← canonical shared master (set via Setup button)
    Backups/                     ← account backup folders (date/name/tdata/)
  tests/                         ← unittest suite (expects to be sibling of the .app)
  manager_config.json            ← auto-generated port mirror for the launcher — do not delete

~/Library/Application Support/TelegramManager/
    manager_data.json            ← per-account metadata (notes, usernames, order, …)
    manager_workspaces.json      ← workspaces
```

**Shared master MUST be Telegram Desktop** (`com.tdesktop.Telegram`, from desktop.telegram.org). The native macOS Telegram (`ru.keepcoder.Telegram`) ignores `TelegramForcePortable` and opens the user's personal session; `_wrong_app_type_error()` rejects it in setup/update.

## Path Resolution (Critical)

`server.py` resolves `ROOT_DIR` and `DATA_DIR` at import time:

```python
APP_BUNDLE = dirname(dirname(dirname(abspath(__file__))))  # the .app bundle
_PARENT_DIR = dirname(APP_BUNDLE)
ROOT_DIR = _PARENT_DIR/TG  if TG/ exists  else _PARENT_DIR
DATA_DIR = _PARENT_DIR/data if data/ exists else _PARENT_DIR
```

The Swift launcher (`launcher.sh`) reads `manager_config.json` from `../../manager_config.json` (relative to MacOS/), which is `PARENT_DIR/manager_config.json`. When `save_config()` writes to `DATA_DIR/manager_config.json`, it also mirrors the `port` key to `PARENT_DIR/manager_config.json` so the launcher always finds it.

## Architecture

**Backend (`server.py`):**
Plain Python `http.server` with `ThreadingMixIn`. No frameworks. All API endpoints are `/api/*`. The server serves `index.html` at `/`.

**Frontend (`index.html`):**
Single-file, no build toolchain. All CSS and JS are inline. Communicates with the backend via `fetch()` to the local server.

**Account model:**
An "account" is a folder containing `TelegramForcePortable/tdata/`. Each account gets its own cloned `Telegram.app` bundle on open (APFS copy-on-write via `cp -cR`), which is removed again when Telegram quits (watcher thread). The shared master lives in `data/_apps/macOS/Telegram.app` or `data/Telegram.app`.

**Shared state files (all atomic-write via `.tmp` + `os.replace`):**
- `data/manager_config.json` — app settings (port, Session Keeper, cache threshold, extra scan dirs)
- `~/Library/Application Support/TelegramManager/manager_data.json` — per-account metadata keyed by absolute folder path (migrated out of `data/`; legacy location read once then removed)

**Key server.py subsystems:**
- `scan_accounts()` / `scan_accounts_cached()` — recursive directory walk with 4 s cache and double-checked locking. Only walks `tdata/` per account (fast); full-disk sizes are handled by the separate disk stats cache.
- `get_disk_stats()` / `_compute_disk_stats()` — slow ROOT_DIR walk for the stats bar, cached 60 s with background refresh. Never blocks the hot scan path.
- `_app_watcher_loop()` — background thread removing idle cloned `.app` bundles; uses a 15 s grace period (`_watcher_exempt`) after `open_account()` to avoid immediate cleanup.
- `open_account()` → returns `(bool, str)` — clones app if missing, strips quarantine, launches via `open -a`.
- `_run_keeper_pass(interval_days, open_secs)` — shared keeper logic used by both the scheduled loop (`run_keeper_loop`) and the manual trigger (`trigger_keeper_now`).
- `_run_as_admin()` — writes shell commands to a temp file and runs via `osascript do shell script ... with administrator privileges` (avoids embedding user-controlled strings in AppleScript).
- `_as_str()` / `_sq()` — AppleScript string escaping helpers (AppleScript has no backslash escapes inside string literals).
- `backup_account()` / `restore_backup()` — crash-safe: backups copy into `<dir>.partial` then atomic-rename; restores copy to `tdata.new` then rename-swap. Backup paths from clients must resolve exactly 2 levels under `Backups/` (`_resolve_backup_dir`).

## WKWebView Constraints

The Swift launcher (`launcher.swift`) hosts the UI in a `WKWebView`. Critical constraints:

- **`window.confirm()` / `alert()` / `prompt()` require `WKUIDelegate`** — without it they silently return `false`/`undefined`. `WKUIDelegate` is now implemented in `launcher.swift` (`runJavaScriptConfirmPanelWithMessage`). Any change to `launcher.swift` requires recompiling the binary (see command above).
- **Do not add new `confirm()` calls for non-critical actions** — prefer removing the guard and relying on toast feedback + clear button labelling. Reserve `confirm()` for truly destructive irreversible actions (e.g. `importConfig` which overwrites all metadata).
- **CSS variables `--danger` and `--card` do not exist** — use `--red` for error/destructive states and `--card-bg` for card backgrounds. Defined CSS variables: `--red`, `--green`, `--yellow`, `--orange`, `--accent`, `--accent-hover`, `--bg`, `--text`, `--text-dim`, `--border`, `--card-bg`, `--card-hover`, `--muted`, `--toolbar-bg`, `--group-accent`, `--group-header-bg`.

## Password Lock

Four config keys added to `manager_config.json` for the lock screen feature:

| Key | Default | Purpose |
|-----|---------|---------|
| `lock_password_hash` | `null` | SHA-256 hex of `salt + password`; `null` = lock disabled |
| `lock_password_salt` | `null` | 16-byte random hex salt |
| `lock_hint` | `""` | Optional hint shown on lock screen |
| `lock_timeout_minutes` | `5` | Idle minutes before auto-lock (0 = never) |

Lock logic lives entirely in `index.html` JS (`initLock`, `lockApp`, `tryUnlock`, `resetIdleTimer`). Called from `boot()` after server is ready.

## Config Keys (`manager_config.json`)

| Key | Default | Purpose |
|-----|---------|---------|
| `port` | 8477 | HTTP server port |
| `extra_scan_dirs` | `[]` | Additional directories to scan for accounts |
| `keeper_enabled` | false | Session Keeper: periodically opens accounts to prevent session expiry |
| `keeper_interval_days` | 30 | Days between keeper opens |
| `keeper_open_seconds` | 120 | Seconds to keep account open during keeper cycle |
| `auto_clear_cache_mb` | 0 | Auto-clear media cache on close if over this size (0 = disabled) |
| `backup_keep_per_account` | 0 | Backups kept per account, oldest pruned (0 = keep all) |
| `app_source` | `""` | Legacy app-source path; set only via the approved picker, empty = auto-detect |

## Size Reporting

- **Per-account card** — shows `tdata_size`: the size of `TelegramForcePortable/tdata/` only (fast, computed during scan).
- **Stats bar** — shows the full `ROOT_DIR` walk (`total_disk`) + `Backups/` folder size + media cache total. These come from `get_disk_stats()` which caches for 60 s and refreshes in background — never slows the account list.
- Sizes use **decimal units** (1 GB = 1,000,000,000 bytes) to match macOS Finder.

## Removed Features

The following features were removed because they were broken or caused session corruption:

- **Device Name** (`apply_device_name`, `_patch_app_for_device_name`) — patched Telegram binary with `dd`, broke tdata by triggering APFS CoW split and ad-hoc re-signing which changed the app's code identity.
- **Grab Usernames** (`grab_usernames`) — AppleScript accessibility scraping; unreliable and not useful.
- **Avatar grabbing** (`grab_avatar_from_window`, `data/avatars/`) — removed; do not re-document.

## Security Patterns

- All paths from API requests are validated through `is_safe_path()` before use — checks that `realpath` resolves within `ROOT_DIR` or `DATA_DIR`.
- Workspace account paths are validated with `is_safe_path()` before saving.
- Shell commands use subprocess list args (never string interpolation) except AppleScript paths, which use `_as_str()` / `_sq()` helpers.
- Admin-privilege shell commands are written to a temp `.sh` file so no user value is ever embedded in the `osascript` string literal.
