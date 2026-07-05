# Sharing TelegramManager with a Friend

This guide explains exactly what to send and what your friend needs to do to run TelegramManager on their Mac.

---

## What to Send

Zip and send the following folder structure. The recipient only needs **TelegramManager.app** and an empty **TG/** folder — they supply their own **Telegram.app**.

```
MyAccounts/                         ← top-level folder (any name)
  TelegramManager.app               ← send this
  TG/                               ← empty folder (accounts will go here)
  data/                             ← config + Telegram master app
    manager_config.json             ← optional, will be auto-created
    Telegram.app                    ← they must provide this themselves
```

> **Do NOT include your TG/ account folders.** Those contain your personal session data (login credentials). Only send the empty folder.

---

## What Your Friend Needs to Do

### Step 1 — Get Telegram.app (portable build)

Download the **macOS portable build** of Telegram from the official source.
It must be the standalone `.app` (not the Mac App Store version — that one is sandboxed and won't work in portable mode).

Place the downloaded `Telegram.app` inside the `data/` folder next to `TelegramManager.app`.

```
MyAccounts/
  TelegramManager.app
  TG/
  data/
    Telegram.app     ← place it here
```

### Step 2 — Allow TelegramManager to Run (Gatekeeper)

Because TelegramManager isn't from the App Store, macOS will block it on first launch.

**Right-click** `TelegramManager.app` → **Open** → click **Open** in the security dialog.
You only need to do this once.

### Step 3 — First Launch (Swift Window Compilation)

On the very first launch, the app compiles its native window (~20 seconds). A notification will appear saying *"Compiling native window…"* and then *"Native window ready!"*

This only happens once. All subsequent launches are instant.

**Requirements:**
- macOS 11 (Big Sur) or newer
- Python 3 — check with `python3 --version` in Terminal. It's pre-installed on modern macOS.
- Xcode Command Line Tools (for the Swift window) — install with: `xcode-select --install`

> **If Xcode is not installed:** The app falls back to opening in Chrome/Brave/Edge as a web app. It works identically, just without the native window frame.

### Step 4 — Create Your First Account

1. Click **New Account** in the toolbar
2. Give it a name (e.g. your phone number or a label)
3. Telegram opens for that account — log in as usual
4. The account is now saved under `TG/` and will appear in TelegramManager every time

---

## Folder Layout After First Use

```
MyAccounts/
  TelegramManager.app
  TG/
    AccountName/
      Telegram.app              ← auto-cloned on open, removed on close
      TelegramForcePortable/
        tdata/                  ← the session data (keep this backed up!)
  data/
    Telegram.app                ← the shared master (never modified)
    manager_config.json
    manager_data.json
    avatars/
    Backups/
```

---

## Features Overview

| Feature | Description |
|---------|-------------|
| Open / Close | Launch or quit Telegram for any account |
| Open All / Close All | Bulk open or close every account at once |
| New Account | Create a fresh account folder and launch Telegram to log in |
| Backup | Copy an account's tdata to `data/Backups/` (safe point-in-time snapshot) |
| Restore | Restore a previous backup into an account |
| Rename / Delete | Rename an account folder or move it to Trash |
| Pin | Keep frequently used accounts at the top |
| Color labels | Color-code accounts for quick identification |
| Notes | Per-account text notes |
| Workspaces | Save a named group of accounts to open together |
| Dock Name | Set the name shown in the macOS Dock when the account is running |
| Session Validity | Check whether an account's session is still active |
| Session Keeper | Auto-open accounts on a schedule to prevent session expiry |
| Diagnose / Repair | Inspect and fix common account problems (lock files, permissions, etc.) |
| Grab Avatar | Screenshot the Telegram window to save a profile picture |
| Auto-clear cache | Automatically delete media cache when an account closes |
| Export / Import Config | Back up and restore all metadata and settings to a JSON file |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Telegram.app not found" on open | Make sure `data/Telegram.app` exists and is the portable build |
| App won't open (Gatekeeper blocked) | Right-click → Open, not double-click |
| No native window, just a browser tab | Install Xcode CLI tools: `xcode-select --install` |
| Port conflict (server won't start) | Edit `data/manager_config.json` and change `"port": 8477` to any free port |
| Account shows "needs setup" | The tdata folder exists but is in the wrong place — use Setup Account |
| Size in stats bar seems slow to update | Disk sizes refresh in the background every 60 s — normal on first load |

---

## Privacy Note

Each account's session lives entirely inside its `TG/<AccountName>/TelegramForcePortable/tdata/` folder.
Deleting that folder logs out the account permanently. Back it up if you want to keep the session.
