# Install Manager and custom AyuGram 1.2.0 on another Mac

These packages are for **Apple Silicon (arm64), macOS 12 or newer**.
They are ad-hoc signed, not Apple-notarized. No account sessions, saved messages,
backups, passwords, VPS credentials or personal configuration are included.

## First installation

1. Install Python 3.9 or newer if needed. The
   [official macOS installers](https://www.python.org/downloads/macos/) include
   an **Install Certificates.command** step for HTTPS; complete it as described
   in the [Python macOS guide](https://docs.python.org/3/using/mac.html).
   Python is an external runtime, not bundled with Manager.
2. Open the [Manager 1.2.0 release](https://github.com/oyenlowery1989-design/TGManager/releases/tag/manager-v1.2.0)
   and download **TelegramManager-1.2.0-macOS-arm64.zip**.
3. Unzip it and move the entire **TelegramManager** folder to a permanent
   writable location. Keep `TelegramManager.app`, `TG/` and `data/` together.
   The native window is precompiled; Xcode and Git are not required for this package.
4. Open `TelegramManager.app`.
5. Open **Client Updates**, check for updates, then choose **Download and Schedule**
   for **AyuGram Custom 1.2.0**. Quit Manager and any managed clients normally.
   The update helper installs the custom application after they close.
6. Reopen Manager and verify the installed custom version in **Client Updates**.
   Its internal application version remains **7.2.9**; **1.2.0** is the custom
   distribution version.
7. Set up the shared **Telegram Desktop** application from
   [desktop.telegram.org](https://desktop.telegram.org/). Create a Telegram account
   in Manager and log in yourself. The current UI requires a ready Telegram account
   before showing AyuGram login actions.
8. On that account, choose **New AyuGram Login** and authenticate yourself.
   This creates a separate AyuGram profile. Subsequent launches use
   **Open with AyuGram** and retain the custom account name.

Do not double-click the standalone custom AyuGram bundle: it requires the
isolated profile and launch guard supplied by Manager. This custom 7.2.9 build
cannot import sessions written by Telegram 7.3.1; use a separate login and keep
the compatibility protection enabled.

## Existing installations

Use **Client Updates** to download and schedule each component separately.
Quit Manager and all managed clients normally for each queued installation.
The helper replaces application bundles only. It does not close clients
forcibly or move account sessions.

Rollback retains one previous application per component. Custom AyuGram rollback
is blocked once the newly installed version is first launched through Manager.
It never restores or replaces a session backup.

The ZIP is for a fresh installation. Do not drag its whole folder over an existing
installation containing accounts; use the in-app updater there.

## VPS history and account data

The new Mac starts without your profiles or saved history. Configure its Google
Cloud access and VPS settings separately, then bind synchronization to the
correct local AyuGram account using **Check Archive IDs** and **Enable Sync**.
VPS history synchronization merges supported saved messages and edits; it does
not copy authentication or provide a complete conversation backup.

For the exact setup and limitations, see
[VPS history synchronization](https://github.com/oyenlowery1989-design/TGManager/blob/codex/ayugram-manager/docs/REMOTE_AYUGRAM.md).

## Release files

- `manager.json` and `manager.tar.gz`: verified Manager updater assets.
- `TelegramManager-1.2.0-macOS-arm64.zip`: first-install folder with the same
  signed Manager bundle, empty data directories and these instructions.
- `ayugram-custom.json` and `ayugram-custom.tar.gz`: verified custom AyuGram
  updater assets, on the
  [AyuGram Custom 1.2.0 release](https://github.com/oyenlowery1989-design/TGManager/releases/tag/ayugram-custom-v1.2.0).
- `SHA256SUMS.txt`: checksums for the assets in each release.

The custom release packages the previously verified binary and complete source
patch; it does not claim a new upstream AyuGram version. Source links, patches,
build instructions and the upstream license accompany the release.
