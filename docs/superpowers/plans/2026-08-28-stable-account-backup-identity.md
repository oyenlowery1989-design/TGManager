# Stable Account Backup Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep backup ownership and retention stable after account-folder renames.

**Architecture:** A lazy UUID is stored in metadata per account path and moves with the existing rename transaction. New backups write a manifest in their partial directory before atomic publication; listing and pruning use its UUID, while manifest-free legacy backups remain visible and restorable.

**Tech Stack:** Python 3 stdlib (`uuid`, `json`, `datetime`), atomic metadata writes, `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-28-stable-account-backup-identity-design.md`

## Global Constraints

- No dependencies or database.
- Do not infer IDs for existing manifest-free backups.
- Write `backup.json` before renaming `.partial` to final.
- Never delete a completed backup to create a replacement.
- Legacy backups remain listable, restorable, and deletable.

---

## File Map

| File | Responsibility |
|---|---|
| `Resources/state.py` | Lazy persisted UUID per account path. |
| `Resources/backups.py` | Manifest, unique publishing, manifest-aware list/prune. |
| `Resources/server.py` | Rename/import metadata handling. |
| `tests/test_server_helpers.py` | Regression coverage. |

### Task 1: Persist a stable account UUID

**Files:** Modify `TelegramManager.app/Contents/Resources/state.py`; test `tests/test_server_helpers.py`.

**Produces:** `ensure_account_id(path) -> str`, lazily persisted under `metadata["account_ids"][path]`.

- [ ] **Step 1: Write the failing test**

```python
def test_ensure_account_id_is_stable_and_persisted(self):
    account = self._account()
    first = state.ensure_account_id(account)
    self.assertEqual(first, state.ensure_account_id(account))
    self.assertEqual(state.metadata["account_ids"][account], first)
    self.assertEqual(len(first), 32)
```

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_server_helpers.ManagedAccountPathTests.test_ensure_account_id_is_stable_and_persisted -q`; expect a missing-helper failure.

- [ ] **Step 3: Implement the minimum helper**

```python
def ensure_account_id(path):
    with _meta_lock:
        ids = metadata.setdefault("account_ids", {})
        account_id = ids.get(path)
        if not isinstance(account_id, str) or not account_id:
            account_id = uuid.uuid4().hex
            ids[path] = account_id
            save_metadata(metadata)
        return account_id
```

Import `uuid`; do not backfill IDs during scan or startup.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command; expect PASS.

- [ ] **Step 5: Commit**

Run `git add TelegramManager.app/Contents/Resources/state.py tests/test_server_helpers.py` then `git commit -m "feat: persist stable account IDs"`.

### Task 2: Publish unique manifest-backed backups

**Files:** Modify `TelegramManager.app/Contents/Resources/backups.py`; test `tests/test_server_helpers.py`.

**Produces:** final paths `Backups/YYYY-MM-DD_HH-MM-SS/<uuid[-N]>` and `backup.json` with `account_id`, `account_name`, and `created_at`.

- [ ] **Step 1: Write the failing tests**

```python
def test_same_second_backups_get_distinct_destinations(self):
    first = backups.backup_account(self.account, "Account")
    second = backups.backup_account(self.account, "Account")
    self.assertTrue(first[0]); self.assertTrue(second[0])
    self.assertNotEqual(first[2], second[2])
    self.assertTrue(os.path.isfile(os.path.join(first[2], "backup.json")))

def test_failed_second_backup_keeps_existing_completed_backup(self):
    first = backups.backup_account(self.account, "Account")
    with mock.patch("backups.os.rename", side_effect=OSError("finalize failed")):
        second = backups.backup_account(self.account, "Account")
    self.assertTrue(first[0]); self.assertFalse(second[0])
    self.assertTrue(os.path.isdir(first[2]))
```

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_server_helpers.BackupPathTests -q`; expect current destination collision/replacement behavior.

- [ ] **Step 3: Implement unique atomic publishing**

Use seconds in the date folder and select a free path before copying:

```python
base = os.path.join(state.DATA_DIR, "Backups", date_folder, account_id)
backup_dir = base
suffix = 2
while os.path.exists(backup_dir) or os.path.exists(backup_dir + ".partial"):
    backup_dir = f"{base}-{suffix}"
    suffix += 1
```

Write UTF-8 manifest JSON after copying tdata into `partial_dir` and before `os.rename(partial_dir, backup_dir)`. Delete the old branch that removes an existing completed `backup_dir`.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command; expect PASS.

- [ ] **Step 5: Commit**

Run `git add TelegramManager.app/Contents/Resources/backups.py tests/test_server_helpers.py` then `git commit -m "feat: publish unique backup manifests"`.

### Task 3: List and prune by UUID while retaining legacy backups

**Files:** Modify `TelegramManager.app/Contents/Resources/backups.py`; test `tests/test_server_helpers.py`.

**Produces:** backup list entries with `account`, `account_id`, and `account_name`; `prune_backups(account_id)` selects only manifest-backed backups for that UUID.

- [ ] **Step 1: Write the failing tests**

```python
def test_list_backups_uses_manifest_name_and_id(self):
    backup = self._make_backup(account="uuid")
    json.dump({"account_id": "abc", "account_name": "Old name",
               "created_at": "2026-08-28T18:30:00"},
              open(os.path.join(backup, "backup.json"), "w"))
    item = server.list_backups()[0]
    self.assertEqual(item["account"], "Old name")
    self.assertEqual(item["account_id"], "abc")

def test_legacy_backup_remains_listed_without_id(self):
    self._make_backup(account="legacy-name")
    item = server.list_backups()[0]
    self.assertEqual(item["account"], "legacy-name")
    self.assertIsNone(item["account_id"])
```

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_server_helpers.BackupPathTests -q`; expect manifest fields to be absent.

- [ ] **Step 3: Implement list/prune migration**

Add `_read_backup_manifest(path)` that returns `None` for missing, malformed, or wrong-typed JSON. A valid manifest supplies visible `account_name` and `account_id`; legacy entries keep the directory name and use `account_id: None`. Change pruning to select `b["account_id"] == account_id`, and call it using the UUID from Task 2.

- [ ] **Step 4: Verify GREEN**

Run `python3 -m unittest tests.test_server_helpers.BackupPathTests tests.test_server_helpers.PruneBackupsTests -q`; expect PASS.

- [ ] **Step 5: Commit**

Run `git add TelegramManager.app/Contents/Resources/backups.py tests/test_server_helpers.py` then `git commit -m "feat: group backups by stable account ID"`.

### Task 4: Preserve IDs through rename and import

**Files:** Modify `TelegramManager.app/Contents/Resources/server.py`; test `tests/test_server_helpers.py`.

**Produces:** rename moves `metadata["account_ids"]`; import accepts only non-empty string account IDs.

- [ ] **Step 1: Write the failing tests**

```python
def test_rename_moves_account_id(self):
    old_path = self._account("old")
    state.metadata["account_ids"] = {old_path: "stable-id"}
    ok, new_path = server.rename_account(old_path, "new")
    self.assertTrue(ok)
    self.assertEqual(state.metadata["account_ids"], {new_path: "stable-id"})

def test_import_rejects_invalid_account_id_value(self):
    payload = valid_export_payload()
    payload["metadata"]["account_ids"] = {"/tmp/account": 12}
    self.assertFalse(server._validate_import_payload(payload)[0])
```

- [ ] **Step 2: Verify RED**

Run `python3 -m unittest tests.test_server_helpers.ManagedAccountPathTests tests.test_server_helpers.ServerHelperTests -q`; expect rename to lose the ID or import to accept an invalid value.

- [ ] **Step 3: Implement the metadata changes**

Add `"account_ids"` to `rename_account()`'s copied sections. Add that section to `_validate_import_payload` and require `dict[str, str]` with non-empty values. Do not alter restore/delete APIs.

- [ ] **Step 4: Verify GREEN**

Run the Step 2 command; expect PASS.

- [ ] **Step 5: Commit**

Run `git add TelegramManager.app/Contents/Resources/server.py tests/test_server_helpers.py` then `git commit -m "fix: preserve account IDs on rename"`.

### Task 5: Verify and record the migration

**Files:** Modify `docs/NEXT_SESSION_HANDOFF.md` and `docs/TODO_TEAM_AUDIT.md`.

- [ ] **Step 1: Run complete verification**

Run `python3 -m unittest discover -s tests -q`, `python3 -m py_compile TelegramManager.app/Contents/Resources/*.py`, and `git diff --check`. Expect all tests, compilation, and whitespace checks to pass.

- [ ] **Step 2: Update handoff and audit notes**

Record the UUID migration, legacy-backup compatibility, test count, and remaining backup transaction work.

- [ ] **Step 3: Commit**

Run `git add docs/NEXT_SESSION_HANDOFF.md docs/TODO_TEAM_AUDIT.md` then `git commit -m "docs: record stable backup identity"`.
