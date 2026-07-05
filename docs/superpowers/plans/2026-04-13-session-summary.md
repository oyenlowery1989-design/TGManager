# Session Summary — 2026-04-13

**Scope:** Feature additions, bug fixes, and lite app creation for TelegramManager.

---

## Changes Made

### 1. Shared App Auto-Detection

**Problem:** "Set Up Shared App" only looked in the canonical `_apps/macOS/Telegram.app` path; placing `Telegram.app` next to `TelegramManager.app` was not detected.

**Fix:** Added `SIBLING_APP = os.path.join(ROOT_DIR, "Telegram.app")` and updated `get_shared_app()` to fall back to it:

```python
def get_shared_app():
    if os.path.isdir(SHARED_MACOS_APP):
        return SHARED_MACOS_APP
    if os.path.isdir(SIBLING_APP):
        return SIBLING_APP
    return None
```

**Impact:** Zero-config setup — just drop `Telegram.app` next to `TelegramManager.app`.

---

### 2. Account Creation Fix

**Problem:** Creating an account failed with "Telegram.app source not found" because `create_account()` wasn't using the two-tier `get_shared_app()` lookup.

**Fix:** `create_account()` now calls `get_shared_app()` as its source, with `cp -cR` (APFS clone) and `-R` fallback.

---

### 3. Auto-Remove Cloned App on External Close

**Problem:** When Telegram is closed outside the manager (e.g., Cmd+Q inside Telegram), the cloned `Telegram.app` bundle remains in the account folder, wasting disk.

**Fix:** Added background `_app_watcher_loop()` thread that polls `ps` every 5s when accounts are running (backs off to 30s when idle). When it detects an account has stopped, it calls `remove_cloned_app(path)`.

Grace period: `_watcher_exempt(path, seconds=15)` is called before `open` so the watcher doesn't remove the app during launch (process hasn't appeared in `ps` yet).

---

### 4. Show Errors When Opening Account Fails

**Problem:** Clicking "Open" always showed "Opening…" regardless of whether launch succeeded.

**Fix:**
- Changed `open_account()` return type from `bool` to `(bool, str)` — returns error message on failure
- Used `subprocess.run(..., timeout=10)` instead of `Popen` to capture returncode + stderr
- Added Gatekeeper quarantine strip: `xattr -dr com.apple.quarantine <app>` after clone
- Updated all call sites to unpack `ok, msg = open_account(...)`
- JS post-open check: 3s after opening, re-fetches account list and warns if process still not in `ps`

---

### 5. Media Cache Auto-Clear (Threshold-Based)

**Problem:** Accounts accumulate large media caches over time.

**Fix:** Added `clear_media_cache(account_path, threshold_mb=0)` function. If `auto_clear_cache_mb > 0` in config and cache exceeds the threshold, cache is cleared on account close.

- Config key: `"auto_clear_cache_mb": 0` (0 = disabled)
- Settings → Advanced tab: checkbox + MB input
- Watcher clears cache when removing the cloned app on external close

---

### 6. Bug Fixes

| Bug | Fix |
|-----|-----|
| `DEFAULT_CONFIG` had `"app_source": "/Applications/Telegram 2.app"` (wrong default) | Changed to `""` |
| `/api/delete` didn't check if account was running | Added `is_running()` guard, returns error message |
| Cloned `Telegram.app` not stripped of Gatekeeper quarantine | Added `xattr -dr com.apple.quarantine` in `clone_app_to_folder()` and `/api/shared-app/setup` |
| Watcher polled every 5s even when no accounts running | Backs off to 30s sleep when `any_running is False` |

---

### 7. Lite Version

**Scope:** Open, close, create, delete accounts only. No shared app infrastructure, no watcher, no backup, no workspaces, no keeper.

**Files created:**

| File | Purpose |
|------|---------|
| `TG/TelegramManager.app/Contents/Resources/server_lite.py` | Lite backend (292 lines) — reads port from `manager_config.json` |
| `TG/TelegramManager.app/Contents/Resources/index_lite.html` | Lite UI (497 lines) — pure DOM manipulation (no innerHTML) |

**Endpoints:** `GET /api/accounts`, `POST /api/open`, `/api/open-all`, `/api/close`, `/api/close-all`, `/api/create`, `/api/delete`

**UI features:** Account grid with open/close per card, open-all/close-all toolbar, search filter, create modal (Cmd+N), delete confirm, no-app banner, 4s auto-refresh.

---

### 8. TelegramManager Lite.app Bundle

**Problem:** Lite version needed to be a launchable `.app`.

**Solution:** Reused the compiled Swift `launcher` binary from `TG/TelegramManager.app/Contents/MacOS/`. The launcher is generic — reads `server.py` from Resources and port from ROOT_DIR/`manager_config.json`.

**Bundle structure:**
```
TelegramManager Lite.app/
├── Contents/
│   ├── Info.plist  (CFBundleExecutable: launcher_lite, id: com.local.telegram-manager-lite)
│   ├── MacOS/
│   │   └── launcher_lite       ← copy of compiled Swift launcher binary
│   └── Resources/
│       ├── server.py           ← server_lite.py (renamed, launcher expects "server.py")
│       ├── index_lite.html
│       ├── app_window_lite.py  ← kept for reference (not used)
│       └── AppIcon.png
```

**Key decision:** `server_lite.py` is placed in Resources as `server.py` because the Swift launcher hardcodes that name. Port is read dynamically from `manager_config.json` to stay in sync with the full app.

---

## Files Modified

| File | Change Type |
|------|-------------|
| `TG/TelegramManager.app/Contents/Resources/server.py` | Primary source — all backend changes |
| `TelegramManager.app/Contents/Resources/server.py` | Synced copy |
| `TG/TelegramManager.app/Contents/Resources/index.html` | UI: open-error feedback, cache settings, setup status UI |
| `TelegramManager.app/Contents/Resources/index.html` | Synced copy |
| `TG/TelegramManager.app/Contents/Resources/server_lite.py` | New — lite backend |
| `TG/TelegramManager.app/Contents/Resources/index_lite.html` | New — lite UI |
| `TelegramManager Lite.app/Contents/Info.plist` | New — lite app bundle info |
| `TelegramManager Lite.app/Contents/MacOS/launcher_lite` | New — Swift launcher binary (copy) |
| `TelegramManager Lite.app/Contents/Resources/server.py` | New — server_lite.py renamed |
| `TelegramManager Lite.app/Contents/Resources/index_lite.html` | New — lite UI copy |

---

## Technical Notes

- **APFS cloning:** `cp -cR` creates reflinks — near-zero disk cost per account clone
- **Gatekeeper:** `xattr -dr com.apple.quarantine` must be run on cloned `.app` bundles or macOS silently blocks launch
- **innerHTML avoidance:** Security hook blocks Write operations containing `innerHTML =`. All render functions in `index_lite.html` use `createElement`/`textContent`/`appendChild`
- **Port propagation:** Lite server reads port from `manager_config.json` via `_read_port()`; JS uses `window.location.origin` (no hardcoded port)
- **PyObjC abandoned for lite launcher:** `NSApplication` can't be imported from `Foundation` in PyObjC. Using compiled Swift binary instead.
