# TelegramManager — Folder & Code Improvement Plan

Generated 2026-07-05 by multi-agent analysis (orchestrator + 3 analysis agents). All claims verified against actual code. **Nothing has been changed yet.**

Current folder is ~11.4 GB effective; after cleanup it drops to ~8 GB (just `TG/` + `data/`), with a much cleaner root.

---

## 1. Folder cleanup

### Safe to delete now (zero code references, verified)
| Item | Size | Why |
|---|---|---|
| `.DS_Store` (root, `TG/`, `data/`, `data/Backups/`) | ~40 KB | Finder noise, gitignored |
| `TelegramManager.app/Contents/Resources/server.py.pre-fix-backup` | 112 KB | Old copy of server.py; git history has it |
| `TelegramManager.app/Contents/MacOS/launcher.stale-backup` | small | Build cruft |
| `TelegramManager.app/Contents/MacOS/launcher_chrome_backup` | small | Build cruft |

These stray backups sitting next to live files are landmines — easy to edit the wrong one.

### Archive out of the working folder (move, don't delete blindly)
| Item | Size | Notes |
|---|---|---|
| `TG.zip` | 2.6 GB | Confirmed full duplicate of `TG/` (Jul 2 snapshot). It's the only *archive* of live login sessions — move to external/cold storage, verify, then remove from here. It alone is >50% of the folder. |
| `TelegramManager.zip` | 112 KB | Old app snapshot (pre password-lock). Superseded by git history. |
| `TelegramManager 2.zip` | 148 KB | Jul 4 snapshot — same content as `data/CodeBackups/TelegramManager.app.20260704-144930/`. Keep one retention mechanism (git), drop the rest. |
| `data/CodeBackups/…20260704-144930/` | 700 KB | Duplicate of the zip above. |
| `data/docs/superpowers/` | small | Duplicates top-level `docs/superpowers/`. Diff, keep the git-tracked copy. |
| `data/README.docx`, `data/TODO.docx` | small | Check against README.md; archive if stale. |

You currently have **three** parallel code-backup mechanisms (git, zips, `data/CodeBackups/`). Pick git — tag releases instead of zipping.

### Move (low risk, no code impact)
- `V2_PROMPT.md`, `SHARING_GUIDE.md` → `docs/` (both plain git-tracked docs; update the README layout section).

### Do NOT move — code depends on exact locations
| Item | Depended on by |
|---|---|
| Root `manager_config.json` | Auto-generated port mirror. Read by `launcher.sh:20`, `launcher.swift:16`, `app_window.py:31`. Deleting it → silent fallback to port 8477 until next config save. Leave it. |
| `TG/` (name and location) | `server.py:30` hardcodes `_PARENT_DIR/TG`. Rename → silent fallback: whole parent dir gets scanned as accounts, and all per-account metadata (notes/pins/proxies, keyed by absolute path) is orphaned. |
| `data/` | `server.py:33` + everything derived: `CONFIG_FILE:36`, log `:48`, `SHARED_APPS_DIR:368`, `SIBLING_APP:376`, Backups (`:765,1306,1341,1367,2060`). Move → config silently resets (port, **lock password**, keeper), shared Telegram master unreachable. |
| `TelegramManager.app` (depth) | Nesting it in e.g. `app/` makes `server.py:29` treat `app/` as PARENT_DIR → app sees zero accounts/config. Same for `launcher.sh:16`, `launcher.swift:16`, `app_window.py:31`. Keep `.app`, `TG/`, `data/` as siblings. |
| `data/Backups/`, `data/_apps/` | Live backup output + shared app master. |

### Target layout
```
TelegramManager_backup_v3/
├── TelegramManager.app/      (unchanged, minus stray backups)
├── TG/                       (unchanged)
├── data/                     (minus CodeBackups dup, docs dup, docx strays)
├── docs/
│   ├── superpowers/…
│   ├── SHARING_GUIDE.md      (moved)
│   └── V2_PROMPT.md          (moved)
├── tests/
├── manager_config.json       (auto-generated — leave)
├── CLAUDE.md / README.md / .gitignore
```
Zips → external archive drive/cloud, not in the repo.

---

## 2. Code changes needed if you reorganize

- **Deleting the three zips: zero code changes** — nothing references them (verified across server.py, launchers, index.html, tests).
- **Moving V2_PROMPT/SHARING_GUIDE to docs/: zero code changes** — only update README's layout section.
- Renaming `TG/` to something else would require editing `server.py:30`; moving `data/` or nesting the `.app` requires touching `server.py:28–33`, `launcher.sh:16,20`, `launcher.swift:16`, `app_window.py:31` in lockstep. **Recommendation: don't — keep the sibling layout.**
- `tests/test_server_helpers.py:8` assumes `tests/` is a sibling of `TelegramManager.app/` — keep it there.

---

## 3. Code & robustness improvements (prioritized)

1. **Make path fallbacks loud.** `ROOT_DIR`/`DATA_DIR` (`server.py:30–33`) silently fall back to PARENT_DIR if `TG/` or `data/` is missing — the app then rescans the wrong tree and resets config (including the lock password) with no warning. Add a startup log warning + a UI banner when a fallback triggers. *Biggest safety win, ~10 lines.*
2. **Split `_do_POST`** (`server.py:2601–3228`, ~628 lines, 38 `elif` branches) into a `ROUTES = {"/api/open": handler, ...}` dict of small functions. Mechanical, low-risk, biggest maintainability win. (Full module split is *not* recommended — tests monkey-patch `server.DATA_DIR` and the file is otherwise well-sectioned.)
3. **Verify Dock Name safety.** `_patch_app_display_name` (`server.py:1771–1794`) uses the same Info.plist-patch + ad-hoc re-sign pattern that CLAUDE.md blames for tdata corruption in the removed Device Name feature. It only touches per-account clones so blast radius is smaller — but verify and document why it's safe, or remove it. *Only item with data-loss risk.*
4. **Sync the two skip-lists.** `SKIP_NAMES` (`server.py:362–365`) and the reserved-names list (`index.html:2574`) are maintained independently and already differ (`_apps`, `Telegram.app`, `tupdates`, `.DS_Store` missing client-side). Serve the list from the backend via `/api/config` instead.
5. **Drop `confirm()` on cache-clear** (`index.html:2148, 2161`) — the dialogs themselves say the action is non-destructive; CLAUDE.md policy says confirm() is for irreversible actions only. Keep it at `:2679` (delete backup) and `:2879` (import config).
6. **Log the config-mirror failure.** `save_config`'s mirror write (`server.py:271–279`) swallows `OSError` silently — the Swift launcher depends on that file; log it.
7. **Tests.** 10 tests pass (`python3 -m unittest discover -s tests`, verified). Biggest gaps: `scan_accounts()`/cache logic and `is_allowed_app_source()` (security-relevant, untested). Use the temp-dir fixture pattern already in `BackupPathTests`.
8. **Fix doc drift.** CLAUDE.md says server.py is ~1,100 lines (actual: 3,280; index.html: 3,666); says `--card` (actual: `--card-bg`); documents `avatars/`/`grab_avatar_from_window()` which no longer exist in code (`data/avatars/` is empty dead weight); no doc says how to run tests. `launcher.swift:14` comment ("two levels up") contradicts its own code. Also: `launcher.swift:30` hardcodes the pgrep pattern `'TelegramManager.app/…/server.py'` — breaks self-detection if the bundle is ever renamed.
9. **`app_window.py` is dead code** when the Swift binary exists — either delete it or add a header comment that it's the fallback only, so its third copy of the config-path logic doesn't silently rot.
10. **Dev workflow (tiny).** Add a one-line `scripts/test.sh`, run `python3 -m py_compile server.py` before commits, and note both in CLAUDE.md. No requirements.txt needed (stdlib-only — correct as-is). CI not worth it for a macOS-only solo app.

---

## Suggested execution order
1. Delete stray backups + .DS_Store (5 min, zero risk)
2. Move zips to external archive, dedupe CodeBackups/docs (needs your archive location)
3. Move the two .md files into docs/, update README
4. Code items #1, #4, #6 (small robustness fixes)
5. Code item #3 (Dock Name verification)
6. Code item #2 (route-table refactor), then #7 (tests), #8–10 (docs/workflow)
