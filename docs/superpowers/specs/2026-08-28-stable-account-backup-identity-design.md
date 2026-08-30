# Stable Account and Backup Identity — Design Spec

_Date: 2026-08-28_

## Goal

Keep an account's backups connected to that account after its folder is
renamed, while preserving the folder name visible when each backup was made.

## Current behavior

Backups are addressed by their folder-name component. After a rename, later
backups use the new name, old backups retain the old name, and per-account
retention can treat them as unrelated accounts.

## Design

Each managed account receives a random UUID stored in metadata under an
`account_ids` map keyed by its absolute path. The ID is created lazily when an
account is backed up and moves with the existing metadata transaction when the
folder is renamed.

Each completed backup receives a small `backup.json` manifest beside `tdata`:

```json
{
  "account_id": "uuid",
  "account_name": "Folder name at backup time",
  "created_at": "ISO-8601 timestamp"
}
```

The on-disk backup directory remains date-based, but its final component uses
the account UUID. A timestamp with seconds and a collision suffix makes every
destination unique. The user-facing backup list reads `backup.json`, grouping
and pruning by `account_id` while displaying the historical `account_name`.

## Migration and compatibility

Existing backups have no manifest. They remain listed using their directory
name and can still be restored or deleted. They are not retroactively assigned
an ID because guessing from a renamed folder is unsafe. The next new backup
establishes the account's stable identity.

Existing metadata without `account_ids` remains valid; the map is added only
when a new ID is needed. Export/import carries the map like other metadata.

## Failure handling

The manifest is written inside the `.partial` staging directory before the
atomic final rename. A failed copy or manifest write leaves no completed backup
and never replaces an existing completed backup. A malformed or absent manifest
is treated as a legacy backup, not an error.

## Tests

- Two backups created in the same second get distinct completed destinations.
- A failed second backup never removes the first completed backup.
- Renaming an account preserves its `account_id`.
- Backups before and after rename group and prune by the same ID.
- Legacy backups without a manifest remain visible and restorable.
