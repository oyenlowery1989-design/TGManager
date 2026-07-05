# Password Lock — Design Spec
_Date: 2026-04-18_

## Overview

Add optional password protection to TelegramManager. When enabled, a full-screen lock screen blocks the UI on every launch and after a configurable idle timeout. The lock is purely frontend — appropriate for the threat model (physical access by someone sitting at your Mac). Running Telegram accounts are unaffected when the screen locks.

## Threat Model

Physical access only: someone sits at an unlocked Mac and opens TelegramManager. Network-level or multi-user attacks are out of scope. A frontend lock with a hashed password is sufficient.

## Data Model

Four new keys added to `manager_config.json`:

| Key | Type | Default | Purpose |
|-----|------|---------|---------|
| `lock_password_hash` | `string \| null` | `null` | SHA-256 hex digest of `salt + password` |
| `lock_password_salt` | `string \| null` | `null` | 16-byte random hex salt, generated once on password set |
| `lock_hint` | `string` | `""` | Optional hint shown on lock screen |
| `lock_timeout_minutes` | `int` | `5` | Minutes of inactivity before auto-lock (0 = never) |

When `lock_password_hash` is `null`, the lock feature is entirely disabled — no lock screen is shown, no idle timer runs.

No new server endpoints needed. These keys flow through the existing `/api/config` GET (returns all config) and POST (saves allowed keys) endpoints. The POST allowlist in `server.py` must include the four new keys.

## Lock Screen UI

A full-screen overlay `#lock-screen` lives at the top of the DOM with `z-index: 9999` and `pointer-events: all`. It is shown/hidden by toggling a CSS class. When visible it completely covers the app — nothing underneath is clickable or readable.

**Contents (centered card):**
- App title: "TelegramManager"
- `<input type="password">` — auto-focused, Enter key triggers unlock attempt
- "Show hint" link — reveals the hint text inline; hidden if no hint is set
- Error feedback — shake animation + red tint on wrong password
- Unlock button

No cancel or close button. The only exit is the correct password.

**Lock triggers:**
1. **On page load** — if `lock_password_hash` is non-null, show lock screen before any content renders
2. **Idle timeout** — a JS timer resets on `mousemove`, `keydown`, `mousedown`, `touchstart`; when it fires, `lockApp()` is called

**Unlock flow:**
1. User types password, presses Enter or clicks Unlock
2. JS runs `crypto.subtle.digest('SHA-256', encoder.encode(salt + input))`
3. Compare resulting hex string to `lock_password_hash`
4. Match → `unlockApp()`: hide overlay, reset idle timer
5. No match → shake animation, clear input, re-focus

## Settings: Security Tab

A new **"Security"** tab is added as the 4th tab in the Settings modal (after Advanced).

### When no password is set

- Info text: "No password set — app is unlocked"
- `[Set Password]` button → reveals inline form

### When a password is set

- Info text: "Password protection is enabled"
- `[Change Password]` button → reveals inline form (includes "Current password" field)
- `[Remove Password]` button → clears all 4 lock keys in config, disables lock

### Inline password form

Fields:
- **Current password** — only shown when changing an existing password
- **New password**
- **Confirm new password**
- **Hint (optional)** — short text shown on lock screen via "Show hint"

Validation (all client-side before saving):
- New password must not be empty
- New password must match confirm field
- If changing: current password must hash-match the stored hash

On save:
1. Generate 16 random bytes → hex string as new salt
2. Hash `salt + newPassword` with `crypto.subtle.digest('SHA-256', ...)`
3. POST to `/api/config` with updated `lock_password_hash`, `lock_password_salt`, `lock_hint`, `lock_timeout_minutes`

### Auto-lock timeout (always visible)

- Label: "Auto-lock after inactivity"
- Number input (minutes), note: "0 = never auto-lock"
- Saved with config regardless of whether a password is set

## Recovery

No automated recovery flow. If the user forgets their password, they can manually remove or edit `manager_config.json` to clear the `lock_password_hash` key. The lock screen UI shows a small note: _"Forgot your password? Edit manager_config.json and remove the lock_password_hash key."_

## Files Changed

| File | Change |
|------|--------|
| `server.py` | Add 4 new keys to `DEFAULT_CONFIG`; add them to the POST `/api/config` allowlist |
| `index.html` | Add `#lock-screen` overlay HTML + CSS; add Security tab to Settings modal; add all JS lock/unlock/idle logic |
