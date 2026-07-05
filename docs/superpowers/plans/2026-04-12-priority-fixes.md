# Priority Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 6 priority issues identified in the April 2026 analysis: shell injection (P0), path traversal (P1), silent logging (P1), port propagation (P2), API error normalization (P2), and non-atomic rename (P2).

**Architecture:** All changes are in `server.py` except port propagation (also touches `launcher.sh` and `launcher.swift`). No new files needed. No external dependencies added.

**Tech Stack:** Python 3, `shlex` (stdlib), `logging` + `RotatingFileHandler` (stdlib), Bash, Swift

---

## File Map

| File | Changes |
|---|---|
| `TelegramManager.app/Contents/Resources/server.py` | Tasks 1–5 |
| `TelegramManager.app/Contents/MacOS/launcher.sh` | Task 6 |
| `TelegramManager.app/Contents/Resources/launcher.swift` | Task 6 |

---

## Task 1: Shell-injection helper + device-name fixes (P0)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py:1–14` (add import)
- Modify: `TelegramManager.app/Contents/Resources/server.py:517–582` (`apply_device_name`)

### Background

Commands are embedded TWO levels deep:
`Python f-string → AppleScript do shell script "..." → shell`

`shlex.quote()` can produce double-quoted strings (`"it's"`) which would break the AppleScript outer string. We need a helper that *always* produces single-quoted shell arguments.

- [ ] **Step 1: Add `shlex` import and `_sq()` helper**

Open `server.py`. The imports block starts at line 1. Add `import shlex` after line 8 (`import subprocess`). Then, after the `DEFAULT_CONFIG` block (around line 34), add the helper function.

**Add `import shlex` — line 8 currently reads `import subprocess`, insert after it:**

```python
import shlex
```

**Add `_sq()` helper after the DEFAULT_CONFIG block (after line 34):**

```python
def _sq(value: str) -> str:
    """Single-quote a shell argument for embedding inside an AppleScript double-quoted string.

    shlex.quote() may emit double-quoted strings which break AppleScript's own double-quote
    delimiters. This always produces single-quoted strings by escaping embedded single quotes
    as: '  →  '\''
    """
    return "'" + str(value).replace("'", "'\\''") + "'"
```

- [ ] **Step 2: Fix `apply_device_name` — `set_cmds` block (lines 546–551)**

The current code embeds `name` and `safe_name` without quoting:
```python
    set_cmds = (
        f"scutil --set ComputerName '{name}' ; "
        f"scutil --set LocalHostName '{safe_name}' ; "
        f"scutil --set HostName '{safe_name}' ; "
        f"hostname '{safe_name}'"
    )
```

Replace with:
```python
    set_cmds = (
        f"scutil --set ComputerName {_sq(name)} ; "
        f"scutil --set LocalHostName {_sq(safe_name)} ; "
        f"scutil --set HostName {_sq(safe_name)} ; "
        f"hostname {_sq(safe_name)}"
    )
```

- [ ] **Step 3: Fix `apply_device_name` restore thread (lines 573–580)**

Current code uses f-string single-quoting for OS-read values passed to `bash -c`:
```python
    def restore():
        time.sleep(30)
        cmds = [
            f"sudo -n scutil --set ComputerName '{orig_computer}'",
            f"sudo -n scutil --set LocalHostName '{orig_local}'",
            f"sudo -n hostname '{orig_local}'",
        ]
        if orig_host:
            cmds.append(f"sudo -n scutil --set HostName '{orig_host}'")
        subprocess.run(["bash", "-c", " ; ".join(cmds) + " 2>/dev/null"], capture_output=True)
```

Replace with (uses `shlex.quote()` — safe here since this goes to `bash -c` directly, not via AppleScript):
```python
    def restore():
        time.sleep(30)
        cmds = [
            f"sudo -n scutil --set ComputerName {shlex.quote(orig_computer)}",
            f"sudo -n scutil --set LocalHostName {shlex.quote(orig_local)}",
            f"sudo -n hostname {shlex.quote(orig_local)}",
        ]
        if orig_host:
            cmds.append(f"sudo -n scutil --set HostName {shlex.quote(orig_host)}")
        subprocess.run(["bash", "-c", " ; ".join(cmds) + " 2>/dev/null"], capture_output=True)
```

- [ ] **Step 4: Verify by reading the modified lines**

Read `server.py` lines 540–585 and confirm:
- `set_cmds` uses `_sq(name)` and `_sq(safe_name)`
- restore thread uses `shlex.quote(orig_computer)` etc.
- `_sq` is defined and `shlex` is imported

---

## Task 2: Shell-injection fix in `apply_proxy` (P0)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py:607–662` (`apply_proxy`)

- [ ] **Step 1: Fix the proxy `set_cmd`/`restore_set` block (lines 634–643)**

Current code (all values unquoted):
```python
    if proxy_type.startswith("socks"):
        set_cmd = f"networksetup -setsocksfirewallproxy '{service}' {host} {port} off"
        on_cmd  = f"networksetup -setsocksfirewallproxystate '{service}' on"
        off_cmd = f"networksetup -setsocksfirewallproxystate '{service}' off"
        restore_set = f"networksetup -setsocksfirewallproxy '{service}' {orig_host} {orig_port} off"
    else:
        set_cmd = f"networksetup -sethttpproxy '{service}' {host} {port}"
        on_cmd  = f"networksetup -sethttpproxystate '{service}' on"
        off_cmd = f"networksetup -sethttpproxystate '{service}' off"
        restore_set = f"networksetup -sethttpproxy '{service}' {orig_host} {orig_port}"
```

Replace with (`_sq()` on every interpolated variable, since these embed in AppleScript):
```python
    if proxy_type.startswith("socks"):
        set_cmd     = f"networksetup -setsocksfirewallproxy {_sq(service)} {_sq(host)} {_sq(port)} off"
        on_cmd      = f"networksetup -setsocksfirewallproxystate {_sq(service)} on"
        off_cmd     = f"networksetup -setsocksfirewallproxystate {_sq(service)} off"
        restore_set = f"networksetup -setsocksfirewallproxy {_sq(service)} {_sq(orig_host)} {_sq(orig_port)} off"
    else:
        set_cmd     = f"networksetup -sethttpproxy {_sq(service)} {_sq(host)} {_sq(port)}"
        on_cmd      = f"networksetup -sethttpproxystate {_sq(service)} on"
        off_cmd     = f"networksetup -sethttpproxystate {_sq(service)} off"
        restore_set = f"networksetup -sethttpproxy {_sq(service)} {_sq(orig_host)} {_sq(orig_port)}"
```

- [ ] **Step 2: Verify by reading lines 607–662**

Confirm all 8 command strings use `_sq()` for interpolated values. The `restore()` inner function at line 653 uses `restore_set`, `on_cmd`, `off_cmd` which are already safe after Step 1.

---

## Task 3: Path validation helper (P1)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py` — add helper after `_sq`, apply in `do_POST`

- [ ] **Step 1: Add `is_safe_path()` helper**

Add after the `_sq()` function (after Task 1's additions, ~line 42):

```python
def is_safe_path(path: str) -> bool:
    """Return True if path resolves to within ROOT_DIR (prevents path traversal)."""
    if not path:
        return False
    try:
        real_path = os.path.realpath(os.path.abspath(path))
        real_root = os.path.realpath(ROOT_DIR)
        return real_path == real_root or real_path.startswith(real_root + os.sep)
    except Exception:
        return False
```

- [ ] **Step 2: Add guards to file-operation POST endpoints**

The endpoints that perform file-system operations (not just metadata writes) need path validation. Find each of the following blocks in `do_POST` and add the guard shown.

**`/api/open` (line ~1528):**
```python
        if path == "/api/open":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok = open_account(acc_path)
            invalidate_scan_cache()
            self.send_json({"success": ok})
```

**`/api/close-account` (line ~1580):**
```python
        elif path == "/api/close-account":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok = kill_account(acc_path)
            self.send_json({"success": ok})
```

**`/api/setup` (line ~1585):**
```python
        elif path == "/api/setup":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg = setup_account(acc_path)
            self.send_json({"success": ok, "message": msg})
```

**`/api/backup` (line ~1594):**
```python
        elif path == "/api/backup":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg = backup_account(acc_path, data.get("name", "account"))
            self.send_json({"success": ok, "message": msg})
```

**`/api/rename` (line ~1641):**
```python
        elif path == "/api/rename":
            old_path = data.get("path", "")
            new_name = data.get("new_name", "").strip()
            if not is_safe_path(old_path):
                self.send_json({"success": False, "message": "Invalid path"})
            elif not new_name or "/" in new_name:
                self.send_json({"success": False, "message": "Invalid name"})
            else:
                ok, result = rename_account(old_path, new_name)
                self.send_json({"success": ok, "new_path": result if ok else "",
                                "message": "" if ok else result})
```

**`/api/restore` (line ~1673):**
```python
        elif path == "/api/restore":
            backup_path  = data.get("backup_path", "")
            account_path = data.get("account_path", "")
            if not is_safe_path(backup_path) or not is_safe_path(account_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            ok, msg = restore_backup(backup_path, account_path)
            self.send_json({"success": ok, "message": msg})
```

**`/api/grab-avatar` (line ~1679):**
```python
        elif path == "/api/grab-avatar":
            acc_path = data.get("path", "")
            if not is_safe_path(acc_path):
                self.send_json({"success": False, "message": "Invalid path"})
                return
            old_ap = get_avatar_path(acc_path)
            if os.path.exists(old_ap):
                os.remove(old_ap)
            ok, msg = grab_avatar_from_window(acc_path)
            self.send_json({"success": ok, "message": msg})
```

- [ ] **Step 3: Verify**

Read `do_POST` and confirm each of the 7 endpoints above has `is_safe_path` guard before any function call.

---

## Task 4: File-based logging (P1)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py:1–14` (add imports)
- Modify: `TelegramManager.app/Contents/Resources/server.py` (add logger setup ~line 22)
- Modify: `TelegramManager.app/Contents/Resources/server.py:1957–1958` (`log_message`)

- [ ] **Step 1: Add logging imports**

After the existing import block (after line 14 `from urllib.parse import urlparse, parse_qs`), add:

```python
import logging
from logging.handlers import RotatingFileHandler
```

- [ ] **Step 2: Set up rotating file logger**

After the `ROOT_DIR` / path constants block (after line 22 `WORKSPACES_FILE = ...`), add:

```python
# ── Logging ──────────────────────────────────────────────────────────────────
_log_file    = os.path.join(ROOT_DIR, "manager.log")
_log         = logging.getLogger("TelegramManager")
_log.setLevel(logging.INFO)
_log_handler = RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3,
                                   encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_log_handler)
```

- [ ] **Step 3: Replace silent `log_message`**

Find the `log_message` method at line ~1957:
```python
    def log_message(self, *args):
        pass
```

Replace with:
```python
    def log_message(self, fmt, *args):
        _log.info("%s - %s", self.address_string(), fmt % args)
```

- [ ] **Step 4: Add key operation log calls**

In `open_account()` (around line 482), add after the `open_account` logic succeeds:
```python
    _log.info("open_account: %s", path)
```

In `backup_account()` (around line 1108), add on success:
```python
    _log.info("backup_account: %s → %s", folder_path, backup_dir)
```

In `apply_device_name()` (around line 558), add after the osascript call succeeds (after `if result.returncode != 0: return`):
```python
    _log.info("apply_device_name: set to %r (permanent=%s)", name, keep_permanently)
```

In `apply_proxy()` (around line 649), add after the osascript call succeeds:
```python
    _log.info("apply_proxy: %s %s:%s", proxy_type, host, port)
```

- [ ] **Step 5: Verify**

Read lines 1–25 of server.py and confirm `import logging`, `from logging.handlers import RotatingFileHandler`, and the `_log` setup are present.
Read `log_message` and confirm it calls `_log.info`.

---

## Task 5: Atomic rename (P2)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py:958–982` (`rename_account`)

- [ ] **Step 1: Rewrite `rename_account`**

Current code modifies global `metadata` in-place *after* the rename. If `save_metadata` fails (disk full, etc.) the folder has a new name but metadata still points to the old name.

Replace the entire `rename_account` function (lines 958–982) with:

```python
def rename_account(old_path, new_name):
    """Rename an account folder and update all metadata keys.

    Order: prepare migration in memory → os.rename → apply + save metadata.
    If os.rename fails, metadata is untouched. If save fails after rename,
    we attempt to roll back the folder rename.
    """
    parent   = os.path.dirname(old_path)
    new_path = os.path.join(parent, new_name)
    if os.path.exists(new_path):
        return False, f'A folder named "{new_name}" already exists'

    # Snapshot what needs migrating BEFORE touching the filesystem
    to_migrate = {}
    for section in ("notes", "usernames", "display_names", "order",
                    "colors", "last_opened", "device_names"):
        d = metadata.get(section, {})
        if old_path in d:
            to_migrate[section] = d[old_path]
    was_pinned = old_path in metadata.get("pinned", [])

    try:
        os.rename(old_path, new_path)
    except Exception as e:
        return False, str(e)

    # Apply migration and persist; roll back folder rename on save failure
    for section, value in to_migrate.items():
        d = metadata.setdefault(section, {})
        d.pop(old_path, None)
        d[new_path] = value
    pinned = metadata.setdefault("pinned", [])
    if was_pinned and old_path in pinned:
        pinned[pinned.index(old_path)] = new_path

    try:
        save_metadata(metadata)
    except Exception as e:
        # Best-effort rollback
        try:
            os.rename(new_path, old_path)
        except Exception:
            pass
        return False, f"Metadata save failed: {e}"

    set_telegram_display_name(new_path, new_name)
    return True, new_path
```

- [ ] **Step 2: Verify**

Read `server.py` lines 958–1000. Confirm:
- `to_migrate` is built before `os.rename`
- metadata is applied only after `os.rename` succeeds
- `save_metadata` failure triggers a rollback `os.rename`

---

## Task 6: Port propagation (P2)

**Files:**
- Modify: `TelegramManager.app/Contents/MacOS/launcher.sh:15`
- Modify: `TelegramManager.app/Contents/Resources/launcher.swift:9,24`

The port is currently hardcoded to `8477` in three places. It should read from `manager_config.json` (in ROOT_DIR, which is 3 levels above Resources).

- [ ] **Step 1: Update `launcher.sh`**

Find line 15:
```bash
PORT=8477
```

Replace with:
```bash
CONFIG_FILE="$RESOURCES/../../../manager_config.json"
PORT=$(python3 -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('port', 8477))
except Exception:
    print(8477)
" "$CONFIG_FILE" 2>/dev/null)
PORT=${PORT:-8477}
```

- [ ] **Step 2: Update `launcher.swift` — kill function**

Find line 9:
```swift
    p.arguments = ["-c", "lsof -ti:8477 2>/dev/null | xargs kill -9 2>/dev/null"]
```

This function runs before `AppDelegate` is initialized, so we need a standalone port-reader. Replace `killExistingServer()` function entirely (lines 6–15):

```swift
func loadPort() -> Int {
    guard let res = Bundle.main.resourcePath else { return 8477 }
    let configURL = URL(fileURLWithPath: res)
        .deletingLastPathComponent()   // Resources → Contents
        .deletingLastPathComponent()   // Contents → TelegramManager.app
        .deletingLastPathComponent()   // TelegramManager.app → ROOT_DIR
        .appendingPathComponent("manager_config.json")
        .standardized
    guard let data = try? Data(contentsOf: configURL),
          let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let port = json["port"] as? Int else { return 8477 }
    return port
}

func killExistingServer(port: Int) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/bash")
    p.arguments = ["-c", "lsof -ti:\(port) 2>/dev/null | xargs kill -9 2>/dev/null"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError  = FileHandle.nullDevice
    try? p.run()
    p.waitUntilExit()
    Thread.sleep(forTimeInterval: 0.25)
}
```

- [ ] **Step 3: Update `AppDelegate` to use dynamic port**

Find line 24 in `launcher.swift`:
```swift
    let port = 8477
```

Replace with:
```swift
    let port = loadPort()
```

Find `applicationDidFinishLaunching` (line 26). The call to `killExistingServer()` on line 28 must now pass the port:
```swift
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        killExistingServer(port: port)
        startPythonServer()
        createWindow()
        setupMenuBar()
        waitForServer(attempt: 0)
    }
```

- [ ] **Step 4: Verify**

Read `launcher.sh` lines 14–22 and confirm the dynamic PORT read.
Read `launcher.swift` lines 1–35 and confirm `loadPort()`, `killExistingServer(port:)`, and `let port = loadPort()`.

Note: `launcher.sh` auto-recompiles `launcher.swift` when the source is newer than the binary (`launcher_swift`). After editing `launcher.swift`, the binary will be recompiled on next launch.

---

## Task 7: Normalize API error responses (P2)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py` — `do_GET` unknown-route and `do_POST` unknown-route

- [ ] **Step 1: Fix GET unknown-route response**

In `do_GET`, at line ~1515–1517:
```python
        else:
            self.send_response(404)
            self.end_headers()
```

Replace with:
```python
        else:
            self.send_json({"success": False, "message": "Unknown endpoint"}, 404)
```

- [ ] **Step 2: Fix POST unknown-route response**

In `do_POST`, at line ~1931–1932:
```python
        else:
            self.send_json({"error": "Unknown endpoint"}, 404)
```

Replace with:
```python
        else:
            self.send_json({"success": False, "message": "Unknown endpoint"}, 404)
```

- [ ] **Step 3: Verify**

Read the `do_GET` else clause and `do_POST` else clause and confirm both use `{"success": False, "message": "..."}`.

---

## Self-Review

**Spec coverage check:**

| Priority Item | Task |
|---|---|
| P0: shlex.quote() on proxy host/port | Task 2 |
| P0: shlex.quote() on device name | Task 1 |
| P1: shlex.quote() on restore threads | Task 1 Step 3 |
| P1: is_safe_path() on API paths | Task 3 |
| P1: Un-silence logging | Task 4 |
| P2: Propagate port config | Task 6 |
| P2: Normalize API error responses | Task 7 |
| P2: Atomic rename | Task 5 |

All 8 items covered. P3 items (workspaces drag-to-create, keeper session verification) intentionally deferred — they require significant UI and architecture work.

**Placeholder scan:** No TBD/TODO/similar patterns found in tasks above.

**Type consistency:** `_sq()` defined in Task 1, used in Tasks 1–2. `is_safe_path()` defined in Task 3, used in Task 3. `loadPort()` defined and used in Task 6. No cross-task type mismatches.
