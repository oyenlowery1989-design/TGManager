# TelegramManager — Team Audit TODO

Updated: 2026-08-31
Scope: initial read-only audit of backend security/data integrity, frontend/native launchers, architecture, reliability, and tests; completion status maintained through PR #18.

## Current assessment

No confirmed critical remote vulnerability, remote-code execution, authentication bypass, or current stored XSS was found. The items below are confirmed functional, integrity, reliability, or defense-in-depth issues that should be addressed before considering the project fully finished or distribution-ready.

## Session status — 2026-08-31

- Phase 1 shared Telegram update/clone lifecycle: **complete and repaired
  live**; verified with a single-account restart and a five-account pinned
  smoke batch.
- Native launcher reliability: **complete and merged to `main`** in PR #1
  (`583a677`).
- Manager app password lock/KDF migration: **deferred by user**; no password
  is needed at this stage.
- Account-operation safety path slice: **implemented and verified locally** on
  branch `lock-kdf`, committed, and merged in PR #2 (`da1bdba`).
- Cache-deletion symlink hardening: **implemented and verified locally** on
  branch `cache-safety` (`8055c31`), committed and merged in PR #3
  (`613f116`).
- Per-account mutation locks: **implemented and verified locally** on branch
  `account-op-locks` (`d6eb2da`), committed and merged in PR #4 (`404d756`).
- Delete/setup serialization: **complete and merged to `main`** in PR #5
  (`f577fb1`).
- Stable account ID migration: **complete and merged to `main`** in PR #6
  (`3cdd070`).
  Legacy backup layouts remain compatible and are never auto-pruned when no
  stable account ID is available.
- PR #7–#10 completed config import rollback, saved-JSON recovery, workspace
  rename updates, per-service/channel proxy recovery, external account safety,
  account-name validation, and drag-refresh safety.
- PR #11–#18 completed native WebView loading/navigation, browser fallback
  cleanup, and multi-window metadata refresh. Account drag ordering has since
  been removed in favor of deterministic natural-name ordering and separate
  global/folder pin states; physical account moves use a transactional action.
  The current regression suite has **130 passing tests**.

## Priority 1 — Account path and operation safety

- [x] Add the initial canonical `is_managed_account_path()` validator.
  - Implemented in `Resources/state.py` and re-exported by `server.py`.
  - Requires a real account under `ROOT_DIR` with
    `TelegramForcePortable/tdata`; rejects symlinked path components and
    symlinked portable/tdata directories.
  - Enforced for backup, restore, cache clear, rename, repair, and delete.
  - Follow-up still open: support validated `extra_scan_dirs` accounts and
    audit every account-specific endpoint.
  - Accept actual accounts discovered under `ROOT_DIR` and configured `extra_scan_dirs`.
  - Reject management paths under `DATA_DIR`, including `Backups/` and `_apps/`.
  - Resolve symlinks/canonical paths before comparison.
  - Use the validator on every account-specific endpoint.
  - Keep `_resolve_backup_dir()` as the separate validator for backup paths.
  - Add regression tests proving external accounts work and management directories are rejected.

- [x] Use one per-account operation lock for every destructive or stateful account operation.
  - Covered by `serialize_account_op()` for open, backup, restore, cache clear,
    rename, repair, setup, and delete.
  - Cover open, backup, restore, rename, delete, setup, and repair as appropriate.
  - Ensure rename cannot move an account while backup/restore is copying or swapping `tdata`.
  - Add concurrency regression tests.

- [x] Harden cache deletion against symlinks.
  - Reject symlinked `user_data*` roots and cache targets.
  - Revalidate each target's `realpath()` immediately before deletion.
  - Require every target to remain inside the account's real `tdata` directory.
  - Implemented in `clear_account_caches()`; regression coverage verifies a
    symlinked top-level cache is skipped and its external target remains.

## Priority 2 — Data integrity and recovery

- [x] Add stable account identity for backup naming and migration.
  - Merged in PR #6 (`3cdd070`); implementation commits include `96a817f`,
    `439e526`, `c67d024`, `dcedde2`, `d49f230`, and `1af7b56`.
  - Legacy backup layouts remain readable and are preserved; auto-pruning is
    disabled for legacy backups without a stable account ID.
  - The UUID boundary is validated on import and backup destinations remain
    contained under `data/Backups`.

- [x] Make backup destinations unique and non-destructive.
  - UUID directories, second-resolution timestamps, and collision suffixes
    keep every completed backup distinct; manifests retain its original name.
  - Backup creation stages into a partial directory and publishes atomically.
  - Regression tests cover same-second retention and duplicate names.

- [x] Separate proxy recovery state by network service and proxy channel.
  - Maintain independent SOCKS and HTTP/HTTPS baseline records.
  - Ensure one restore cannot delete another channel's recovery marker.
  - Preserve enough state to recover every outstanding proxy change after a crash.
  - Add concurrent SOCKS/HTTP and crash-recovery tests.

- [x] Make configuration import transactional.
  - Stage and validate metadata, config, and workspaces before committing.
  - Roll back all files and in-memory state if any write fails.
  - Add injected-failure tests for each commit stage.

- [x] Update workspaces when an account is renamed.
  - Replace the old absolute path in every saved workspace.
  - Commit folder rename, metadata, and workspace changes consistently.
  - Consider stable account UUIDs in a later data-model migration so Finder moves do not orphan metadata.

- [x] Validate persisted JSON schemas during startup.
  - Reject valid JSON with the wrong top-level type, such as `null`, arrays, or scalars.
  - Quarantine malformed files, load safe defaults, log the problem, and show a UI warning.
  - Add startup recovery tests for malformed config, metadata, and workspace files.

## Priority 3 — Authentication and native boundary hardening

- [ ] Migrate password hashes from salted SHA-256 to a password KDF.
  - Prefer `hashlib.scrypt()` or PBKDF2 with strong parameters.
  - Store a versioned hash format.
  - Transparently migrate the existing SHA-256 hash after a successful unlock.

- [x] Fix Swift launcher process ownership.
  - Merged in PR #1; release signing remains intentionally deferred.
  - Remove the broad hard-coded `pgrep` pattern.
  - Target the exact bundle/server path or use a bundle-scoped PID/ready file.
  - Do not terminate servers belonging to another installed copy.

- [x] Restrict WKWebView navigation.
  - Allow the main frame only to the authenticated `127.0.0.1` origin, configured port, and token prefix.
  - Reject unexpected redirects/navigation.
  - Open explicitly supported external links outside the privileged WebView.

- [x] Add HTTP defense-in-depth headers.
  - Add `Referrer-Policy: no-referrer`.
  - Add `X-Content-Type-Options: nosniff`.
  - Design a workable Content Security Policy; inline handlers may need refactoring first.

- [x] Consolidate account-name validation.
  - Reuse one validator for create and rename.
  - Reject control characters and all problematic filename characters consistently.
  - Enforce a reasonable maximum length.

## Priority 4 — Frontend, fallbacks, and distribution

- [x] Remove account-card and group drag ordering.
  - Accounts now use natural-name ordering within each folder.
  - Global pin remains cross-folder; folder pin is a separate per-folder
    priority state.
  - Physical moves are exposed through the account action menu.

- [x] Repair or remove obsolete fallback launchers.
  - Update `launcher_chrome.sh` for the token-prefixed URL.
  - Never kill an arbitrary process merely because it owns port 8477.
  - Fix or remove `Revert_to_Chrome.command` references to nonexistent files.
  - Decide whether `app_window.py` is supported; wire it into the fallback chain or remove it.

- [x] Improve browser fallback lifecycle.
  - Use a per-instance temporary browser profile.
  - Stop the server when the fallback browser exits.
  - Avoid exposing the token through Safari history when possible.

- [ ] Establish a repeatable signed release process.
  - Do not mutate the distributed app bundle through runtime compilation.
  - Treat signing failures as real failures rather than suppressing them.
  - Produce a consistently signed/notarized artifact before distributing to other users.

- [x] Fix multi-window stale note/username behavior.
  - Overlay local values only while an edit is actually pending.
  - Allow server-side/imported changes to appear when the local field is clean.

## Test backlog

- [x] External `extra_scan_dirs` account actions.
- [ ] Rejection of `DATA_DIR/Backups`, `_apps`, and other non-account paths.
- [ ] Successful end-to-end backup and restore using disposable data.
- [ ] Same-minute and colliding-name backups.
- [ ] Rename during open/backup/restore/delete.
- [x] Workspace behavior after rename.
- [x] Concurrent SOCKS and HTTP proxy application/recovery.
- [ ] Keeper scheduling and manual-run lifecycle.
- [x] Import failure and rollback at every write stage.
- [x] Malformed persisted JSON startup recovery.
- [ ] Swift and browser launcher startup, failure, port conflict, and multi-copy behavior.
- [x] Frontend group-drag behavior during the five-second refresh.

## Verification baseline

At the time of the audit:

- 105/105 tests passed.
- All Python sources passed compilation checks.
- Shell scripts passed syntax checks.
- `Info.plist` passed validation.
- Git whitespace checks passed.

Current verification (2026-09-02): 121 tests, Python compilation, and Git
whitespace checks pass after the clone-lifecycle and drag/drop fixes.
