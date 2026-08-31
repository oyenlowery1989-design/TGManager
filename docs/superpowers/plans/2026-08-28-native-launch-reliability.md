# Native Launch Reliability Implementation Plan

> **Status (2026-08-28): COMPLETE.** Implemented, compiled, manually smoke
> tested, and merged to `main` in PR #1 (`583a677`). Release signing and
> notarization remain intentionally excluded.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the native launcher wait for the exact Python server it started and report actionable startup failures.

**Architecture:** Swift creates one per-launch temporary ready-file path and passes it to Python through `TG_READY_FILE`. Python writes an atomic JSON record after binding; Swift validates its token, uses the reported port, and owns only the `Process` it launched. No global process kill or release-signing work is included.

**Tech Stack:** Swift/Cocoa/WebKit/Foundation; Python 3 stdlib `http.server`, `json`, `os`; existing `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-28-native-launch-reliability-design.md`

## Global Constraints

- Keep the existing token-only HTTP routing unchanged.
- Use no dependencies and no persistent launch-state files.
- Ready data must be atomic, token-validated, and never logged with its token.
- Do not kill standalone `server.py` processes.
- Do not change release signing/notarization.

---

## File Map

| File | Responsibility |
|---|---|
| `TelegramManager.app/Contents/Resources/server.py` | Write and remove a ready record around the bound server lifecycle. |
| `TelegramManager.app/Contents/Resources/launcher.swift` | Create, pass, parse, poll, and remove the per-launch ready file; retain child diagnostics. |
| `tests/test_server_helpers.py` | Verify ready-record validation, atomic replacement, and cleanup using temporary paths. |

### Task 1: Server ready-file lifecycle

**Files:**

- Modify: `TelegramManager.app/Contents/Resources/server.py` near `ThreadedHTTPServer` and `__main__`.
- Test: `tests/test_server_helpers.py`.

**Consumes:** environment variable `TG_READY_FILE`; existing `PORT`, `ROUTE_PREFIX`, `TG_SESSION_TOKEN` behavior.

**Produces:** `_write_ready_file(path, port, token)` and `_remove_ready_file(path)`; a JSON record `{pid, port, session_token}`.

- [x] **Step 1: Write failing tests**

  ```python
  def test_ready_file_is_atomic_and_contains_bound_server_identity(self):
      ready = os.path.join(self.tmp, "ready.json")
      server._write_ready_file(ready, 8477, "test-token")
      self.assertEqual(json.load(open(ready)), {
          "pid": os.getpid(), "port": 8477, "session_token": "test-token",
      })
      self.assertFalse(os.path.exists(ready + ".tmp"))

  def test_ready_file_cleanup_ignores_missing_path(self):
      server._remove_ready_file(os.path.join(self.tmp, "missing.json"))
  ```

- [x] **Step 2: Run the focused tests and confirm RED**

  Run:

  ```sh
  python3 -m unittest tests.test_server_helpers.ReadyFileTests -q
  ```

  Expected: `AttributeError` because neither helper exists.

- [x] **Step 3: Implement the minimum helpers**

  ```python
  def _write_ready_file(path, port, session_token):
      if not path:
          return
      temporary = path + ".tmp"
      try:
          with open(temporary, "w", encoding="utf-8") as f:
              json.dump({"pid": os.getpid(), "port": port,
                         "session_token": session_token}, f)
          os.replace(temporary, path)
      except OSError as e:
          _log.warning("Could not write launcher ready file: %s", e)
          try:
              os.remove(temporary)
          except OSError:
              pass

  def _remove_ready_file(path):
      if not path:
          return
      try:
          os.remove(path)
      except FileNotFoundError:
          pass
      except OSError as e:
          _log.warning("Could not remove launcher ready file: %s", e)
  ```

  In `__main__`, read `TG_READY_FILE`, call `_write_ready_file` only after
  `ThreadedHTTPServer(("127.0.0.1", port), RequestHandler)` returns, and call
  `_remove_ready_file` from `finally` after `serve_forever()` exits.

- [x] **Step 4: Verify GREEN**

  ```sh
  python3 -m unittest tests.test_server_helpers.ReadyFileTests -q
  python3 -m py_compile TelegramManager.app/Contents/Resources/server.py
  ```

- [x] **Step 5: Commit**

  ```sh
  git add TelegramManager.app/Contents/Resources/server.py tests/test_server_helpers.py
  git commit -m "feat: publish server readiness to launcher"
  ```

### Task 2: Swift ready-file launch and diagnostics

**Files:**

- Modify: `TelegramManager.app/Contents/Resources/launcher.swift`.

**Consumes:** Task 1 ready JSON and existing `sessionToken`.

**Produces:** `readyFileURL`, parsed ready endpoint, and an inline error reason without sensitive values.

- [x] **Step 1: Implement the smallest launcher flow**

  - Remove `readPort()`, `killExistingServer(port:)`, and the `port` property.
  - Add `readyFileURL` under `NSTemporaryDirectory()` using `sessionToken` and
    a UUID; remove any stale file before launch.
  - Pass `TG_READY_FILE` and `TG_SESSION_TOKEN` in `Process.environment`.
  - Append child stdout/stderr to `data/manager.log` via `FileHandle` instead
    of `nullDevice`.
  - Poll the ready file every 0.2 seconds. Validate the token and load the URL
    built from its port. If `serverProcess.isRunning` becomes false first,
    show an “exited before ready” message; after 12 seconds, show a “did not
    publish readiness” message.
  - In `applicationWillTerminate`, terminate only `serverProcess` and remove
    `readyFileURL`.

- [x] **Step 2: Compile the native launcher**

  ```sh
  swiftc TelegramManager.app/Contents/Resources/launcher.swift \
    -o /private/tmp/launcher_swift_check \
    -framework Cocoa -framework WebKit -framework Foundation -O
  ```

- [x] **Step 3: Commit**

  ```sh
  git add TelegramManager.app/Contents/Resources/launcher.swift
  git commit -m "feat: coordinate launcher with server readiness"
  ```

### Task 3: End-to-end verification

**Files:**

- Modify only if prior checks expose a defect.

**Consumes:** Tasks 1 and 2.

- [x] **Step 1: Run full static verification**

  ```sh
  python3 -m py_compile TelegramManager.app/Contents/Resources/*.py
  python3 -m unittest discover -s tests -q
  git diff --check
  ```

- [x] **Step 2: Manual lifecycle smoke test**

  1. Launch `TelegramManager.app`.
  2. Confirm the window loads its session-token URL.
  3. Quit the manager.
  4. Confirm its child `server.py` exits and a separately started server (if
     present) remains untouched.

- [x] **Step 3: Commit any corrective-only diff**

  ```sh
  git add TelegramManager.app/Contents/Resources/server.py \
    TelegramManager.app/Contents/Resources/launcher.swift \
    tests/test_server_helpers.py
  git commit -m "fix: complete native launcher startup verification"
  ```

## Self-Review

- Spec coverage: Tasks 1 and 2 cover atomic readiness, scoped process
  ownership, diagnostics, and focused tests; Task 3 covers the manual lifecycle
  check. Release signing is intentionally absent.
- Placeholder scan: no unresolved requirements or deferred implementation
  markers remain.
- Interface check: Swift consumes only the JSON fields defined by Task 1;
  Python does not depend on Swift-specific paths beyond `TG_READY_FILE`.
