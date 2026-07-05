# TelegramManager — Code Review Findings

**Date:** 2026-07-05
**Method:** 3 parallel review agents — backend (`server.py`), frontend (`index.html`), launcher/architecture (`launcher.sh`, `launcher.swift`, `app_window.py`, config scheme)
**Note:** Line numbers are approximate.

---

## 1. Critical — Fix First (HIGH)

### 1.1 Authentication is effectively optional
- **Where:** `server.py` ~350, 2602; `launcher.sh` 26–32
- The server relies on `TG_SESSION_TOKEN` for URL routing. If the token ends up empty (python3 `secrets` fallback fails, then `uuidgen` fails), **all API endpoints are unauthenticated** on `127.0.0.1`.
- The Origin check applies only to POST and does not prevent DNS-rebinding attacks; no CSRF token on state-changing endpoints (backup, restore, delete).
- **Fix:** Fail loudly at startup if the token is empty. Validate the token on **every** request, not just via route prefix. Add strict Origin/Host validation on all methods.

### 1.2 Port race between launcher and server on relaunch
- **Where:** `launcher.sh` 19–24; `server.py` 264–279; fallback port 8477 hard-coded in 3 places (`launcher.sh`, `launcher.swift`, `app_window.py`)
- Launcher reads the mirrored `PARENT_DIR/manager_config.json` before the server writes it. Change port in-app → quick relaunch → launcher and UI wait on the new port while the server binds the old one → timeout, blank window.
- **Fix:** Server writes the *actual bound port* to a dedicated ready-file (e.g. `port.txt` or `manager_ready.lock`) after binding; launcher polls that file instead of the config mirror. Optionally auto-increment on port conflict.

### 1.3 Restore TOCTOU / symlink exposure
- **Where:** `restore_backup()`, `server.py` ~1421–1459
- `account_path` is validated with `is_safe_path()` early, but not re-validated immediately before the copy. A symlink placed at `account_path/TelegramForcePortable/tdata` could redirect the restore write outside ROOT_DIR/DATA_DIR — `is_safe_path()` resolves the account path itself, but not the final destination after appending.
- **Fix:** Re-run `is_safe_path()` + `os.path.realpath()` on the **final tdata destination** immediately before the copy. Consider rejecting symlinks inside account folders.

### 1.4 Password lock is client-side only (bypassable)
- **Where:** `index.html` (`initLock`, `tryUnlock`); config keys in `manager_config.json`
- Hash/salt verification happens entirely in browser JS. Anyone who can reach the HTTP port (or open dev tools) bypasses the lock — it protects nothing at the API level.
- **Fix:** Enforce server-side: server refuses `/api/*` calls until an unlock endpoint validates the password and issues a session flag. Or explicitly document the lock as cosmetic.

### 1.5 Missing swiftc → silent degraded fallback
- **Where:** `launcher.sh` 56–100
- Without Xcode CLT, compilation fails; the error goes only to `data/launcher_compile.log` and the user silently gets a Chrome window (then Safari as last resort — untested if Chrome absent). `app_window.py` (PyObjC fallback) exists but is never used in the chain.
- **Fix:** Surface the failure reason to the user; insert `app_window.py` into the fallback chain: Swift → PyObjC → browser.

### 1.6 No integration tests
- Only `test_server_helpers.py` (~31 lines) exists. Launch chain (port conflict, missing python3, slow first scan), backup/restore round-trip, and path-validation edge cases are untested.
- **Fix:** Add integration tests for these paths before any further feature work.

---

## 2. Important — Fix Soon (MEDIUM)

| # | Finding | Where | Fix |
|---|---------|-------|-----|
| 2.1 | `importConfig` (destructive: overwrites all metadata) relies on `window.confirm()` with no in-page modal fallback — a WKUIDelegate regression means silent no-op or silent overwrite | `index.html` | Custom HTML modal for destructive confirms |
| 2.2 | 5-second auto-refresh races user edits: note textareas and drag-reorder state can be clobbered mid-interaction | `index.html` render loop | Pause refresh while an input is focused or a drag is active |
| 2.3 | Scan cache invalidation sets `data = None` but not `ts = 0.0` — stale-timestamp window under concurrent access | `server.py` 1005–1016 | Zero the timestamp on invalidation |
| 2.4 | `rename_account()` updates in-memory metadata even if the disk write fails — memory/disk divergence until restart | `server.py` 1846–1882 | Update memory only after confirmed write |
| 2.5 | Python discovery inconsistent: `launcher.sh` tries bare `python3` then hard-coded paths; `launcher.swift` only hard-coded paths. Conda/pyenv users fail cryptically | `launcher.sh` 28–32; `launcher.swift` 78–81 | Single shared `find_python` helper; log resolved path + version |
| 2.6 | `codesign --force --deep --sign -` on every recompile with errors suppressed (`2>/dev/null \|\| true`) — surprise Gatekeeper prompts, hidden signing failures | `launcher.sh` 73 | Sign only when binary changed; log failures |
| 2.7 | Chrome-fallback curl loop times out after ~6 s; slow first scan → blank browser page | `launcher.sh` 94–100 | Longer/adaptive wait, or ready-file from 1.2 |
| 2.8 | `pgrep -f server.py` path match: two copies of the app kill each other's servers | `launcher.sh` 38–42 | Instance UUID in config instead of path match |
| 2.9 | Config mirror asymmetry: only `port` is mirrored, but launcher reads the whole file — manual edits to the mirror are silently ignored | `server.py` 271–279 | Replace mirror with the port-only ready-file (1.2) |
| 2.10 | PS process cache (1 s TTL) can serve stale PIDs right after open/close | `server.py` 946–974 | Invalidate on open/close events |
| 2.11 | Lock hint shown without any authentication attempt | `index.html` lock screen | Show hint only after ≥1 failed attempt |

---

## 3. Minor (LOW)

- `_run_as_admin()` timeout produces a generic error with no indication of which operation timed out (`server.py` 112–130); hard-coded 120 s timeout.
- Account displayName sanitization only strips `/` and `.` — long names / odd characters could confuse Info.plist patching (`server.py` ~1880).
- Legacy metadata migration succeeds silently even if the old file can't be deleted (`server.py` 287–293).
- `_resolve_backup_dir()` rejects paths with trailing slashes / non-normalized input — normalize with `os.path.normpath()` first (`server.py` 1360–1372).
- Backup/restore crash-safety assumes atomic `os.rename()` — true on APFS, not guaranteed on NFS/SMB mounts.
- App-clone lifecycle (clone on open, watcher removes on quit) undocumented for end users.
- Quarantine stripped on clones but not on the master app at setup time.

---

## 4. Recommended New Features

**Safety (highest value)**
1. **Soft-delete / trash** — deleted accounts move to `Backups/.trash_<date>/` for 7 days before permanent removal. Biggest protection against irreversible data loss.
2. **Backup encryption** — tdata grants full account access; backups are plaintext copies. Optional AES/GPG (`.tar.gz.gpg`) on the existing backup pipeline.
3. **Audit log** — timestamped log of every destructive operation (backup, restore, delete, rename).

**Robustness**
4. **`/api/health` endpoint** — uptime, cache stats, watcher status, Python path, port; launcher shows a diagnostics page on startup failure instead of a generic alert.
5. **Operation queue** — serialize backup/restore/setup instead of rejecting with "busy".
6. **Dev mode** — env var to skip Swift compile, use prebuilt binary, verbose stdout logging.

**UX / scale**
7. **Virtualized account list** — current rendering degrades at 500+ accounts.
8. **Multi-instance support** — instance UUID so two app copies coexist (see 2.8).
9. **Config schema** — single definition of keys/defaults/types validated on every write.

---

## 5. Suggested Order of Work

1. Mandatory session token + server-side lock enforcement (1.1, 1.4) — turns security from cosmetic to real
2. Port ready-file (1.2, 2.7, 2.9) — kills the blank-window class of bugs
3. Restore path re-validation (1.3) + soft-delete (feature 1)
4. Destructive-action modals + refresh/edit race (2.1, 2.2)
5. Integration tests (1.6)
6. Remaining MED items, then features
