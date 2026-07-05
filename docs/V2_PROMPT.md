# Prompt: Build Telegram Account Manager v2.0 (from scratch)

Copy everything below into a fresh Claude Code session in an empty directory.

---

Build **Telegram Account Manager v2.0** for macOS — a local app to manage many portable Telegram Desktop accounts, each stored as a folder containing `TelegramForcePortable/tdata/`. This is a from-scratch rewrite of a v1 app that worked but corrupted sessions intermittently. The root causes are known and listed below as **non-negotiable design rules** — the architecture must make those failure modes impossible, not just less likely.

## Core idea

- Accounts live in a root folder (`TG/`), one subfolder per account, each with its own `TelegramForcePortable/tdata/`.
- To open an account, the app gives it its own copy of `Telegram.app` (APFS copy-on-write clone, `cp -cR`) next to its tdata, strips quarantine, and launches it. A shared "master" `Telegram.app` lives in `data/_apps/macOS/`.
- UI: local web frontend served by a Python backend, hosted in a native Swift `WKWebView` window.
- Features from v1 to keep: account list with notes/pins/colors/groups, per-account tdata size + global disk stats bar, open/close, backups, Session Keeper (periodically opens accounts so sessions don't expire), media-cache auto-clear, per-account SOCKS proxy, password lock screen, workspaces (open a set of accounts together), extra scan directories.

## Non-negotiable design rules (each one killed v1 — do not reintroduce)

**Process lifecycle**
1. **Never delete a cloned `.app` based on inference.** v1 had a watcher that polled `ps` and `rm -rf`'d "idle" bundles; a stale grace-period snapshot, a slow cold launch (>15 s), or a single transient `ps` failure made it delete the bundle of a *running or launching* Telegram → tdata corruption. v2: track the exact PID at launch time, reap the clone only after `waitpid`-style confirmation that the PID exited (plus an `lsof` check on the bundle). A failed/empty `ps` means "unknown — do nothing," never "nothing running."
2. **Refuse to open an account that is already running.** Check by tracked PID before cloning/launching. Two Telegram instances on one tdata is the classic corruption. Launch with `open -n <specific-bundle-path>` so LaunchServices never activates the wrong instance (all clones share a bundle ID).
3. **Refuse rename/move/re-sign/backup/restore/delete while the account's Telegram is running.** One shared guard used by every mutating endpoint.

**Code signing**
4. **Never re-sign per open.** v1 ran `codesign --force --deep --sign -` on every clone (return code ignored): broke APFS CoW sharing (disk bloat), often left invalid signatures Gatekeeper killed mid-write → truncated tdata. v2: sign the master once at setup; clones inherit. If a per-account Dock name is truly needed, find a way that doesn't touch code identity (or drop the feature — it's cosmetic).

**Data integrity**
5. **Stable account identity.** v1 keyed all metadata (notes, proxy, order, last-opened) by absolute path: Finder moves orphaned everything, and delete+recreate at the same path silently inherited the old account's proxy — new logins routed through a dead/foreign SOCKS. v2: write a `.tm_id` (UUID) file into each account folder; key all metadata by that ID. GC metadata for IDs no longer found on scan.
6. **All state files atomic** (`.tmp` + `os.replace`) and every read-modify-write under one lock per file. v1's workspaces endpoint lost updates.
7. **Backup retention.** v1 never pruned: `Backups/` + leftover `tdata.bak.*` filled the disk until Telegram's own tdata writes failed → corruption. v2: keep N newest per account, exclude `media_cache` from backups, GC `.bak` folders after confirmed restore.

**Proxy**
8. **No system-wide proxy toggling.** v1 set the macOS global SOCKS proxy and restored it via a detached `nohup sleep 35` — sleep/reboot left all system traffic broken, and two proxied opens within 35 s captured the wrong "original." v2: prefer launching Telegram with per-app proxy config if feasible; if system proxy is unavoidable, persist the original state to disk once, serialize apply/restore, and reconcile at server startup.

**Subprocesses**
9. **Every subprocess call has a timeout** (osascript admin prompts included — v1 hung request threads forever on an unanswered password dialog) and every return code is checked or explicitly logged.
10. Shell safety as in v1: subprocess list args only; admin commands written to a 0700 temp file run via `osascript do shell script`, never interpolated into AppleScript strings.

**Launcher / frontend**
11. **One launcher artifact.** v1 shipped three divergent launchers and `Info.plist` pointed at the oldest — every source fix was dead code; `confirm()` was broken and the port was hardcoded in the stale binary. v2: `CFBundleExecutable` → a shell script that (re)compiles `launcher.swift` when the source is newer, **fails loudly** (show stderr, never silently exec a stale binary), then execs the binary.
12. Swift window implements **`WKUIDelegate`** from day one (confirm/alert/prompt). Still minimize `confirm()` — reserve for irreversible actions.
13. **Server-ready handshake, fail visible.** Launcher polls a `/api/ping`; on timeout it loads an inline error page in the WKWebView ("server failed to start" + log tail), never a silent blank window. Single source of truth for the port (one config file both server and launcher read).
14. **Frontend degrades loudly.** If the server dies mid-session, show a "disconnected — retrying" banner, don't render stale data silently. Lock screen **fails closed**: if the lock config can't be fetched, show the lock and retry — never boot unlocked. Be honest that the lock is UI privacy only, or additionally require the password server-side for sensitive endpoints.
15. Keeper: one mutex shared by scheduled loop and manual trigger; first pass runs immediately on enable, not after the first interval.
16. `extra_scan_dirs` must be added to the path-safety allowlist (realpath-resolved) — v1 listed those accounts but rejected every action on them, including backup.

**Security patterns to carry over from v1 (they were sound)**
- `is_safe_path()`: realpath both sides, prefix test with `os.sep`, allowlist of bases.
- No `innerHTML` with unescaped data; keep an `esc()` helper; prefer DOM methods.
- Decimal size units (1 GB = 1e9) to match Finder.

## Tech constraints

- macOS only, Apple Silicon + Intel. Python 3 stdlib backend (`http.server` + `ThreadingMixIn` is fine — no frameworks), single-file `index.html` frontend (no build toolchain), Swift `WKWebView` window. No external dependencies unless truly necessary.
- Structure the backend in modules this time (v1 was one 2500-line file): `server.py` (routing), `accounts.py` (scan/open/lifecycle), `state.py` (config/metadata with locks + atomic writes), `backups.py`, `keeper.py`, `proxy.py`, `applescript.py` (escaping helpers).
- Log to `data/manager.log` with rotation; every destructive operation logs what/why/result.

## Process

1. Start with a short written plan: module layout, account lifecycle state machine (closed → launching → running → closing → closed, with who transitions what), and the PID-tracking design from rule 1. Get my sign-off on the lifecycle design before writing code.
2. Then build in this order: state layer → scan → open/close lifecycle with PID tracking → UI skeleton → backups → keeper → proxy → lock → polish.
3. Write tests for the pure-logic parts (path safety, state read/write concurrency, retention policy, lifecycle transitions with a fake process table).
