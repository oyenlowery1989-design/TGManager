# Transfer selected data to another Mac

Open **Settings → Advanced → Transfer to Another Mac**. This is a selective transfer between independent logins. It does not move Telegram authorization or create, open, close, or log in to accounts.

On the source Mac:

Quit AyuGram normally for the selected profiles before transferring their history or preferences. Organization-only export does not require closing clients. Keep profiles closed until the operation finishes; the Manager never closes them for you.

1. Choose **History**, **Supported settings**, and/or **Organization**.
2. Select the accounts to export. Each account's catalog shows available numeric archive account IDs and deleted/edited counts. When including history, explicitly choose its archive ID.
3. Click **Download selected accounts**. In the native app, choose a new filename in the macOS save dialog; an existing file is never replaced. Move the saved JSON file securely to the destination Mac.

On the destination Mac:

1. Prepare corresponding accounts through **New account**, choosing the destination folders yourself. Log in independently to the same Telegram accounts. Initialize AyuGram before importing history and close it before transfer.
2. Open **Transfer to Another Mac** and choose the JSON file. Validation and preview do not import anything.
3. Select the source rows to import. For each row, choose an existing local destination account using its folder path. Names are descriptive only; the tool never chooses a destination by matching a name. Each selected destination must be unique.
4. For each history row, review or edit the numeric account ID as text, then check the statement that you independently verified the same Telegram account in the destination login. Changing the destination or ID clears this confirmation. A freshly initialized history database also needs this explicit numeric confirmation.
5. Choose the categories to include, then click **Import selected mappings**. Review the per-account results and imported, skipped, conflict, and ambiguity counts.

The original Settings dialog remains open behind the transfer dialog. Its unsaved form is preserved. Close the transfer dialog before saving those settings.

## What is included

- **History:** supported text records for deleted messages and edit history, associated with the selected numeric archive account ID. Existing local values win conflicts. Unsupported media or reply payloads fail export safely without changing the source. This is not a transfer of every Telegram chat or every possible archive record.
- **Supported settings:** save deleted messages, save message history, global ghost mode and its global settings, translation provider, show peer ID, and semitransparent deleted messages. Source values replace corresponding supported destination preferences. Arbitrary AyuGram settings are not transferred.
- **Organization:** notes, usernames, colors, Dock names, global pins, folder pins, and workspaces. Nonempty destination fields remain intact; blank fields can be filled. Pins and workspace memberships merge. Account folders are not moved or renamed.

Authorization keys, sessions, cache, media/files, VPS credentials, and local-read markers are excluded. Transfer JSON may contain private message text and notes; keep it private. The package limit is **64 MiB**; each history archive is limited to **32 MiB and 100,000 records**.

## Errors and retries

Invalid or oversized files fail validation. A failed preview clears the previous package and mappings, so an old selection cannot be imported accidentally. A bulk import may finish some rows before another fails. Review the result for every account; retrying the same mappings is safe, and existing local history remains authoritative on conflicts.

History imports use SQLite transactions and retain a private archive recovery copy. Settings and organization retain private recovery files under `data/Mac Transfer Backups/` and roll back normal write failures. A power loss between separate file writes can require recovery from those files; the transfer is not one transaction across every account and file. These recovery files stay on the destination Mac and are not included in exports.

The dialog holds its selected package only in memory. Closing it or locking Manager clears the package, preview, and mappings. Closing is temporarily blocked while an operation is running. Locking still clears the dialog; an import already accepted by the server may finish, so review its effects before retrying. The downloaded JSON remains wherever you saved it until you remove it yourself.
