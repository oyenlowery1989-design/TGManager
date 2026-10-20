# Install Telegram Manager 1.3.0

For **Apple Silicon (arm64), macOS 12 or newer**, with Python 3.9 or newer.
The native window is precompiled; Git and Xcode are not required. This package
is ad-hoc signed, not Apple-notarized. No sessions, saved messages, backups,
passwords, VPS credentials, or personal settings are included.

## Update an existing Manager

1. Open **Client Updates** and check for updates.
2. Choose **Download and Schedule** for **Manager 1.3.0**.
3. Quit Manager and all managed Telegram/AyuGram applications normally. The
   installer waits until they are closed and replaces only the Manager app.
4. Reopen Manager yourself. The transfer tool is under
   **Settings → Advanced → Transfer to Another Mac**.

Update Manager on both Macs before transferring data. Existing account
directories, settings, sessions, and backups stay in place. Do not replace an
existing installation with the entire first-install ZIP folder.

One previous Manager app is retained for rollback in the same
`manager-state-v1` compatibility domain. Rollback restores the application,
not profile data. See the
[updater and rollback guide](https://github.com/oyenlowery1989-design/TGManager/blob/manager-v1.3.0/docs/MANAGED_RELEASES.md).

## First installation on another Mac

1. Install Python 3.9 or newer if needed. The
   [official Python macOS installers](https://www.python.org/downloads/macos/)
   include an **Install Certificates.command** step for HTTPS; complete it.
2. Download **TelegramManager-1.3.0-macOS-arm64.zip** from the
   [Manager 1.3.0 release](https://github.com/oyenlowery1989-design/TGManager/releases/tag/manager-v1.3.0).
3. Unzip it and move the entire **TelegramManager** folder to a permanent
   writable location. Keep `TelegramManager.app`, the empty `TG/`, and the
   empty `data/` together.
4. Open Manager. Use **Client Updates** to install **AyuGram Custom 1.2.0**;
   quit the Manager and managed clients normally for installation, then reopen
   Manager yourself. The existing custom AyuGram release supports this transfer
   feature; a new AyuGram binary is not required.
5. Configure the shared Telegram Desktop application from
   [desktop.telegram.org](https://desktop.telegram.org/). Create account folders
   and log in independently on this Mac. Use **New AyuGram Login** for separate
   AyuGram authentication and initialize its profile yourself.

Custom AyuGram's distribution version is 1.2.0 and its upstream application base
is 7.2.9. It cannot import Telegram 7.3.1 sessions; keep compatibility protection
enabled. Launch custom AyuGram through Manager, not by double-clicking its
standalone app bundle.

## Selective transfer

Quit the selected AyuGram profiles normally before exporting or importing their
history/preferences. Organization-only transfer does not need to close clients.
On the source Mac, choose the accounts and categories in **Transfer to Another
Mac**, select archive IDs when including history, and save the JSON file. In the
native app, select a new filename; existing files are never overwritten.

On the destination Mac, choose that JSON, review the preview, map each selected
source account to an existing local profile, and explicitly confirm the numeric
account ID for history. The tool never opens or logs in to an account.

- Supported deleted text and edit history merge without duplicates; local
  values win conflicts. Unsupported media/reply payloads stop export safely.
- Supported AyuGram preferences include global Ghost Mode, deleted/edit history
  saving, translation provider, peer IDs, and deleted-message transparency.
- Notes, Dock names, colors, usernames, pins, and workspaces transfer with paths
  mapped to the chosen profiles. Existing nonempty organization fields stay.
- Authentication, media/cache, VPS credentials, and local-read markers are
  excluded. The JSON contains private history/notes; move and store it privately.

See **MAC_TRANSFER.md** included with this release for limits, partial-import
results, recovery copies, and retries. VPS connections and history bindings must
be configured separately on the new Mac.

## Release assets

- `manager.json` and `manager.tar.gz`: in-app updater assets.
- `TelegramManager-1.3.0-macOS-arm64.zip`: first-install folder with the same signed
  Manager app, empty data directories, and instructions.
- `INSTALL.md` and `MAC_TRANSFER.md`: installation and transfer guides.
- `SHA256SUMS.txt`: SHA-256 checksums for the other assets.

AyuGram Custom is distributed separately in the
[1.2.0 custom release](https://github.com/oyenlowery1989-design/TGManager/releases/tag/ayugram-custom-v1.2.0).
