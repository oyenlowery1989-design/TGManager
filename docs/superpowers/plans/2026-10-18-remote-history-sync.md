# VPS to local AyuGram history synchronization

> **For agentic workers:** Use superpowers:executing-plans for this approved implementation; the parent coordinates bounded specialist work and independent review.

**Goal:** Show the VPS's saved deleted-message text and edit history in the existing local custom AyuGram, without duplicate imports or replacing login sessions.

**Architecture:** Manager obtains a consistent, allowlisted JSON snapshot for one explicitly bound numeric Telegram account over the existing authenticated SSH control helper. A Python archive module validates and transactionally merges into an existing local `ayudata.db` while the local client is fully closed. Source and destination remain independent sessions. The current custom client already reads compatible archive rows on startup, so no new application build is necessary.

**Tech stack:** Python standard library, SQLite, existing HTTP/HTML Manager UI, existing Google Cloud SSH and fixed VPS helper.

**Spec:** User-approved in-chat design: VPS collects continuously; Manager synchronizes saved history into local AyuGram; preserve sessions and avoid duplicates; never open AUS 3.

## Global constraints

- Never launch AUS 3 or any real account for testing; synthetic profiles only.
- Never copy, replace, decode, or modify Telegram authentication/session files.
- Bind by a numeric account ID explicitly confirmed by the user and present in both archives; names do not establish identity.
- No automatic binding. Users enable a destination from Manager before any real history import.
- Import only into the isolated custom AyuGram archive, under the existing account lock and fresh process checks. Never auto-quit an application.
- Preserve existing message content, settings, read markers, and unrelated database tables. Create one private, consistent rolling archive-only recovery backup before mutation.
- UI and stored operator-facing messages are English.
- Preserve source-compatible text/entities. Do not copy or follow remote attachment paths. Unsupported payloads fail explicitly instead of silently truncating.
- Full snapshots have bounded resource limits (32 MiB and 100,000 records); over-limit archives fail visibly. A digest prevents retransmission/reimport when unchanged. No cursor/reset heuristics.

## Review focus

1. Wrong-account and stale bindings must never import unrelated rows or infer a current login from a folder name.
2. Replays, restored source databases, interrupted imports, and repeated equal edit text must neither duplicate imports nor discard distinct revisions.
3. Edits captured on both devices need deterministic ordering without rewriting their payloads; same-second ambiguities must be reported.
4. Links, triggers, unknown schema, malformed entities/JSON, and over-limit responses must preserve the destination.
5. Concurrent launch, locked Manager, remote outage, backup failure, and process-inspection failure must defer or fail without opening clients.

## Task 1: Archive protocol and atomic merger

**Files:** new `TelegramManager.app/Contents/Resources/history_archive.py`, `tests/test_history_archive.py`.

**Interfaces:** `catalog(path) -> dict`; `export_archive(path, user_id, known_digest='') -> dict`; `merge_archive(path, envelope, source_id, backup_path, check_closed) -> dict`. Numeric IDs are strings at the API boundary. Standalone module has no Manager imports and is also deployed to the VPS. Constants define the allowlisted pinned schema and resource limits.

- [x] Write and run failing synthetic SQLite tests for owner isolation, schema validation, repeat sync, overlapping local/source edits, duplicate occurrences, source reset, payload conflicts, rollback, unsafe files, and private backups.
- [x] Implement consistent read snapshots and digest negotiation; stable logical deletion keys; edit sequence merging; independent namespaced receipt table in the same transaction; safe ordering of affected revisions only.
- [x] Run focused tests and verify additional receipt tables survive pinned `sqlite_orm::sync_schema(true)` using synthetic data.

## Task 2: Manager orchestration and VPS transport

**Files:** new `Resources/remote_history.py`, `tests/test_remote_history.py`; modify `remote_ayugram.py`, `server.py`, `ayugram.py`, `tools/ayugram_remote/runtime.py`, `ayugram-remote-control`, `install.py`, and deployment tests.

**Interfaces:** GET `/api/remote-history/status` (local state only); POST `/api/remote-history/catalog`, `/configure`, `/sync`, `/disable`. `remote_history.before_open(path)` attempts enabled synchronization before a user-requested launch. `run_monitor()` handles enabled destinations every five minutes only when Manager is unlocked.

- [x] Write failing isolated tests for opt-in binding, matching archive IDs, unsafe destinations, exact running/busy checks, schema mismatch, transfer limits/timeouts, configuration persistence, recovery backup, and no session file changes.
- [x] Add bounded fixed-command SSH request with stdin JSON; deployable helper supports only catalog and history-export actions against its fixed archive path.
- [x] Implement explicit per-account bindings, background synchronization, visible results/deferred state, and pre-launch catch-up. Keep network/state locks outside account-lock acquisition to avoid inversion.
- [x] Register authenticated, lock-gated endpoints and no-touch status polling. No test sends real history or opens a real account.

## Task 3: UI, review, deployment and verification

**Files:** `Resources/index.html`, `docs/REMOTE_AYUGRAM.md`, UI regression tests.

- [x] Add a History Sync section to Remote AyuGram: destination selection, explicit numeric account ID confirmation, enable/disable, Sync Now, last result, imported/skipped/conflict counts and running-client deferral.
- [x] Test UI with synthetic responses; inspect a synthetic browser preview without opening account apps.
- [x] Run the complete unittest suite and Python/shell syntax checks; review final diff independently and fix findings.
- [x] Deploy only reviewed helper code to the existing VPS without client restart or session changes; verify catalog/protocol and existing service health without reading message contents into logs.
- [x] Reload Manager safely with bindings disabled; verify controls/status. Commit and push only code/docs/tests as the configured git user.

## Known source limits

Pinned source `d4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5` stores archive schema version 1 in plaintext SQLite. Media serialization is unfinished upstream. The custom inline renderer restores ordinary text messages, excluding unsupported topic/flag cases; imported rows can still be available in the history panel. Archive user IDs establish historical ownership, not proof of the currently active Telegram login. The user confirms the intended numeric account ID. No authentication test on AUS 3 is authorized.

## Implementation rulings

- `imported_digest(path, source_id, user_id)` reads the destination receipt; configuration timestamps never suppress imports after database restoration. Binding UUIDs persist across disable/re-enable of the same source/account.
- Local open-file checks supplement exact process identity checks, and pre-launch code repeats its process check after a potentially slow network transfer.
- The database/snapshot limit is 32 MiB; journal/WAL/SHM files are bounded separately at 64 MiB combined to permit normal SQLite duplication of pages. Transaction page allocation is checked before commit.
- Unexpected unique constraints and inbound foreign keys are rejected to prevent replacement/cascade side effects while inserting or rekeying history.
- An unchanged snapshot preserves prior import counts/conflict notices. Missing/moved profiles produce visible monitor errors.

- Final independent review found three preservation/deduplication issues; all fixed with regression coverage, including canonical mention identity without changing serialized payloads.
- Deployment was code-only and verified the actual catalog transport with no service restarts. The VPS archive is currently empty, so no real binding/import was attempted. Manager was reloaded with all sync bindings disabled and no local account clients running.
- Verification: 462 regression tests; separate focused Python 3.9 checks; Python/shell/JavaScript syntax checks; synthetic UI interaction.
