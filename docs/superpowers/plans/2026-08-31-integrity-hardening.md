# Integrity Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent partial local state changes and stale recovery references while tightening the native local-only boundary.

**Architecture:** Keep the existing stdlib JSON files and atomic writes. Add a minimal transaction helper that snapshots file bytes and in-memory dictionaries, then restores both on failure. Key proxy recovery files by service plus channel; retain the old single-file record only as a startup-compatible legacy record.

**Tech Stack:** Python stdlib, unittest, Swift/WebKit.

**Spec:** Audit approved in conversation on 2026-08-31.

## Global Constraints

- Preserve existing persisted data and legacy readers.
- No password KDF work or signing/notarization.
- Add one observable regression test before each production behavior change.
- Use atomic writes and preserve owner-only file permissions.

---

### Task 1: Validate and quarantine persisted JSON

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/state.py`
- Test: `tests/test_server_helpers.py`

**Interfaces:**
- Produces: loaders that return defaults only when decoded JSON has the expected top-level type, moving rejected files to a timestamped `.corrupt-*` sibling.

- [ ] **Step 1: Write failing tests**

```python
def test_load_config_quarantines_valid_json_with_wrong_top_level_type(self):
    # write [] to a temporary CONFIG_FILE, call load_config()
    # assert returned config is DEFAULT_CONFIG-shaped and original is renamed
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python3 -m unittest tests.test_server_helpers.PersistedJsonTests -q`

- [ ] **Step 3: Add expected-type validation and quarantine**

```python
def _load_json_file(path, default_value):
    value = json.load(open(path))
    if not isinstance(value, type(default_value)):
        _quarantine_json_file(path)
        return copy.deepcopy(default_value)
    return value
```

- [ ] **Step 4: Run focused and complete Python tests**

Run: `python3 -m unittest discover -s tests -q`

### Task 2: Make configuration import all-or-nothing

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/state.py`
- Modify: `TelegramManager.app/Contents/Resources/server.py`
- Test: `tests/test_server_helpers.py`

**Interfaces:**
- Consumes: normalized metadata/config/workspaces dictionaries.
- Produces: `import_persisted_state(metadata, config, workspaces)` which either persists all three and updates globals, or restores every changed file and global.

- [ ] **Step 1: Write a failing failure-injection test**

```python
def test_import_rolls_back_metadata_and_config_when_workspace_save_fails(self):
    # patch the final persistence write to raise, call import endpoint helper
    # assert all files and globals still equal their pre-import values
```

- [ ] **Step 2: Run it and verify it fails by retaining an earlier write**

Run: `python3 -m unittest tests.test_server_helpers.ImportTransactionTests -q`

- [ ] **Step 3: Add the minimal transaction helper**

```python
def import_persisted_state(new_meta, new_cfg, new_workspaces):
    # snapshot bytes/existence and deep-copy globals; write all; restore snapshots on error
```

- [ ] **Step 4: Route `_post_api_import_config` through it and run all tests**

Run: `python3 -m unittest discover -s tests -q`

### Task 3: Keep workspaces consistent across account renames

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py`
- Test: `tests/test_server_helpers.py`

**Interfaces:**
- Consumes: `old_path`, `new_path`, persisted workspaces.
- Produces: rename which rewrites each matching workspace account path and rolls the folder rename back if either metadata or workspaces cannot persist.

- [ ] **Step 1: Write a failing workspace rename test**

```python
def test_rename_rewrites_workspace_account_paths(self):
    # create account and workspace containing old path; rename; reload workspace; assert new path
```

- [ ] **Step 2: Run it and verify it fails with the old path retained**

Run: `python3 -m unittest tests.test_server_helpers.RenameWorkspaceTests -q`

- [ ] **Step 3: Extend rename’s existing local-copy/rollback transaction**

```python
new_workspaces = _replace_workspace_account_path(load_workspaces(), old_path, new_path)
save_metadata(new_meta)
save_workspaces(new_workspaces)
```

- [ ] **Step 4: Run focused and complete tests**

Run: `python3 -m unittest discover -s tests -q`

### Task 4: Isolate proxy recovery records

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/proxy.py`
- Modify: `TelegramManager.app/Contents/Resources/state.py`
- Test: `tests/test_server_helpers.py`

**Interfaces:**
- Produces: `_proxy_recovery_path(service, channel)` and startup recovery over every per-service/channel record.

- [ ] **Step 1: Write failing tests**

```python
def test_proxy_recovery_paths_differ_by_service_and_channel(self):
    assert _proxy_recovery_path("Wi-Fi", "socks") != _proxy_recovery_path("Ethernet", "http")
```

- [ ] **Step 2: Run and verify the shared-file implementation fails**

Run: `python3 -m unittest tests.test_server_helpers.ProxyRecoveryTests -q`

- [ ] **Step 3: Use contained encoded filenames and recover all records**

```python
path = os.path.join(state.DATA_DIR, "proxy_recovery", f"{quote(service)}-{channel}.json")
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m unittest discover -s tests -q`

### Task 5: Restrict the native navigation boundary and add headers

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/launcher.swift`
- Modify: `TelegramManager.app/Contents/Resources/server.py`
- Test: `tests/test_http_layer.py`

**Interfaces:**
- Produces: WebView navigation limited to `http://127.0.0.1:<configured port>/<session token>/...`; external links open in the system browser. All HTTP responses include `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.

- [ ] **Step 1: Write the failing HTTP header test**

```python
def test_json_responses_include_local_security_headers(self):
    response = self._get("/api/accounts")
    self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
```

- [ ] **Step 2: Run it and verify headers are absent**

Run: `python3 -m unittest tests.test_http_layer.HTTPLayerTests.test_json_responses_include_local_security_headers -q`

- [ ] **Step 3: Add a shared Python header helper and narrow Swift policy**

```swift
if action.targetFrame?.isMainFrame == true, action.request.url?.host != "127.0.0.1" {
    NSWorkspace.shared.open(action.request.url!)
    decisionHandler(.cancel)
    return
}
```

- [ ] **Step 4: Compile Swift, run Python checks, and run all tests**

Run: `swiftc TelegramManager.app/Contents/Resources/launcher.swift -o /tmp/launcher-check -framework Cocoa -framework WebKit -framework Foundation -O && python3 -m py_compile TelegramManager.app/Contents/Resources/*.py && python3 -m unittest discover -s tests -q`
