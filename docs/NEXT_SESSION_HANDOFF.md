# TelegramManager — Next Coding Session Handoff

Updated: 2026-09-02

## Current state

The current working tree contains uncommitted lifecycle fixes, deterministic
folder ordering, folder-only pinning, and account move support.
Restart TelegramManager before manually testing them; the Python server is not
hot-reloaded.

- PR #7 (`a2825cc`) — config-import rollback, JSON recovery, workspace rename,
  proxy recovery records, WebView boundary, and response headers.
- PR #8 (`45f446e`) — safe account actions for configured `extra_scan_dirs`.
- PR #9 (`3d1d709`) — shared account-name validation.
- PR #10 (`7c06bfe`) — no polling refresh during account or group drags.
- PR #11–#13 — native WebView stops opening a browser and reliably loads the
  local app window.
- PR #14–#16 — authenticated browser fallback lifecycle; obsolete fallback
  scripts removed.
- PR #17 — cached, token-scoped WebView navigation policy.
- PR #18 — multi-window note/username refresh preserves only active or
  pending local saves.

Latest verification: 130 tests passed; Python syntax, JavaScript syntax, and
diff checks passed.

## Resume

1. Run `git status --short --branch`; preserve the current uncommitted fixes.
2. Preserve `AGENTS.md` and `VersionBackups/` if they remain untracked.
3. This handoff and `docs/TODO_TEAM_AUDIT.md` are now tracked.

## Next work

1. Add end-to-end coverage for backup/restore, proxy recovery, keeper, and
   native/browser startup.
2. Release signing/notarization when preparing a distributable build.

## Deferred

- Password KDF: no app password is configured or needed.
- Apple signing/notarization: wait until a distributable release.

## Workflow rule

Every code change: branch from current `main`, verify, commit, push, create a
PR to `main`, merge it, and fast-forward local `main`. Keep working through
routine implementation and PR steps without waiting for another “continue”; stop
only for a blocker or a user decision.
