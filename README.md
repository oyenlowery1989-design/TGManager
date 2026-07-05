# TelegramManager

Manage many portable Telegram accounts on macOS from one window — open, close, back up, and keep sessions alive without juggling app copies by hand.

Native macOS app: Python backend (`http.server`, zero dependencies), single-file HTML/JS frontend, hosted in a Swift `WKWebView` window. No frameworks, no build toolchain, no Electron.

## How it works

- Each **account** is a folder in `TG/` containing `TelegramForcePortable/tdata/` (Telegram's portable session data).
- One shared master `Telegram.app` lives in `data/_apps/`. Opening an account **APFS-clones** the master into the account folder (copy-on-write — instant, ~0 extra disk), launches it, and removes the clone again after Telegram quits. ~435 MB saved per account.
- The master is quarantine-stripped and code-signed **once** at setup — clones stay byte-identical, so macOS never re-prompts and session data never breaks from identity changes.

## Features

| | |
|---|---|
| Accounts | Open/close (single, all, or saved workspaces), create, rename, delete, pin, color labels, notes |
| Backups | Point-in-time tdata snapshots, browse/filter/restore/delete from UI, optional auto-retention (keep newest N), backup age shown on each card |
| Session Keeper | Auto-opens idle accounts every N days so sessions never expire; per-card due indicator; manual run |
| Health | Session validity check, diagnose/repair (lock files, permissions), orphan cleanup |
| Privacy | Password lock screen (SHA-256, salted, idle auto-lock), per-account proxy storage |
| Housekeeping | Auto-clear media cache on close over a size threshold, config export/import |

## Requirements

- macOS 11+ (APFS volume recommended for instant clones)
- Python 3 (pre-installed on modern macOS)
- Xcode Command Line Tools for the native window (`xcode-select --install`) — without them the app falls back to a Chrome/Brave/Edge app window
- Telegram **portable** build (standalone `.app`, not the Mac App Store version — that one is sandboxed and can't run in portable mode)

## Setup

```
MyAccounts/                  ← any folder name
  TelegramManager.app        ← this repo's app bundle
  TG/                        ← accounts live here (starts empty)
  data/
    Telegram.app             ← put the portable Telegram build here
```

1. Place `Telegram.app` in `data/` (or pick any location later via Settings → *Choose App…*).
2. Right-click `TelegramManager.app` → **Open** (Gatekeeper, first time only).
3. First launch compiles the Swift window (~20 s, one time).
4. **New Account** → name it → log in. Done.

## Security model

- Server binds to `127.0.0.1` only, with a per-launch random URL token.
- All request paths validated against the accounts/data roots before use.
- Shell commands use list arguments, never string interpolation; admin-privileged commands go through a user-only temp script so no user value is embedded in AppleScript.
- Proxies are stored per account and shown on the card; system-wide apply is **off by default** and, when enabled, the pre-change proxy state is persisted and auto-restored at next start if a restore was interrupted.
- Session data (`TG/`, `data/`) is never committed — see `.gitignore`.

## Repository layout

```
TelegramManager.app/Contents/
  MacOS/launcher.sh          entry point: compiles Swift window, falls back to browser
  Resources/server.py        backend (~2700 lines, stdlib only)
  Resources/index.html       frontend (single file, inline CSS+JS)
  Resources/launcher.swift   native WKWebView window
docs/                        design docs
tests/                       backend helper tests
SHARING_GUIDE.md             step-by-step guide for sending the app to someone
```

Each account's session lives entirely in its `tdata/` folder — deleting it logs that account out permanently. Back it up.
