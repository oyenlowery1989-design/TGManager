# Password Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional password lock to TelegramManager that shows a full-screen overlay on launch and after configurable idle timeout, managed from a new Security tab in Settings.

**Architecture:** Purely frontend lock — password hash (SHA-256 + random salt) stored in `manager_config.json`; all lock/unlock logic lives in `index.html` JS using `crypto.subtle`. Server only stores/returns the four new config keys. No new endpoints needed.

**Tech Stack:** Vanilla JS (`crypto.subtle`, `crypto.getRandomValues`), Python `http.server` (existing), single-file HTML+CSS frontend.

---

## File Map

| File | Change |
|------|--------|
| `TelegramManager.app/Contents/Resources/server.py` | Add 4 keys to `DEFAULT_CONFIG`; add them to POST `/api/config` allowlist (line ~2151) |
| `TelegramManager.app/Contents/Resources/index.html` | Add lock screen CSS + HTML; add Security tab HTML; add all lock JS; update `showSettings()` + `saveSettings()`; update `boot()` |

---

## Task 1: Extend server.py config

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/server.py`

- [ ] **Step 1: Add 4 new keys to `DEFAULT_CONFIG`**

  Find this block (around line 45):
  ```python
  DEFAULT_CONFIG = {
      "app_source":            "",
      "port":                  8477,
      "extra_scan_dirs":       [],
      # Session Keeper
      "keeper_enabled":        False,
      "keeper_interval_days":  30,
      "keeper_open_seconds":   120,
      # Media cache: auto-clear on close if cache exceeds this size (MB). 0 = disabled.
      "auto_clear_cache_mb":   0,
  }
  ```

  Replace with:
  ```python
  DEFAULT_CONFIG = {
      "app_source":            "",
      "port":                  8477,
      "extra_scan_dirs":       [],
      # Session Keeper
      "keeper_enabled":        False,
      "keeper_interval_days":  30,
      "keeper_open_seconds":   120,
      # Media cache: auto-clear on close if cache exceeds this size (MB). 0 = disabled.
      "auto_clear_cache_mb":   0,
      # Password lock
      "lock_password_hash":    None,
      "lock_password_salt":    None,
      "lock_hint":             "",
      "lock_timeout_minutes":  5,
  }
  ```

- [ ] **Step 2: Add the 4 keys to the POST `/api/config` allowlist**

  Find this block (around line 2151):
  ```python
  for k in ("app_source", "extra_scan_dirs",
            "keeper_enabled", "keeper_interval_days", "keeper_open_seconds",
            "auto_clear_cache_mb"):
  ```

  Replace with:
  ```python
  for k in ("app_source", "extra_scan_dirs",
            "keeper_enabled", "keeper_interval_days", "keeper_open_seconds",
            "auto_clear_cache_mb",
            "lock_password_hash", "lock_password_salt", "lock_hint",
            "lock_timeout_minutes"):
  ```

- [ ] **Step 3: Verify server starts cleanly**

  ```bash
  python3 TelegramManager.app/Contents/Resources/server.py &
  sleep 1
  curl -s http://localhost:8477/api/config | python3 -m json.tool | grep lock
  kill %1
  ```

  Expected output (4 lock keys present):
  ```
  "lock_hint": "",
  "lock_password_hash": null,
  "lock_password_salt": null,
  "lock_timeout_minutes": 5,
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/server.py
  git commit -m "feat: add lock config keys to DEFAULT_CONFIG and /api/config allowlist"
  ```

---

## Task 2: Add lock screen CSS

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Add CSS for the lock screen overlay**

  In the `<style>` block, find the `/* Right-click context menu */` comment (around line 690). Insert the following CSS block **before** it:

  ```css
  /* ── Lock screen ────────────────────────────────────────────────────────── */
  #lock-screen {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 9999;
    background: rgba(15,15,20,0.97);
    align-items: center;
    justify-content: center;
    pointer-events: all;
  }
  #lock-screen.visible {
    display: flex;
  }
  .lock-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 36px 40px 32px;
    width: 340px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    box-shadow: 0 8px 40px rgba(0,0,0,0.6);
  }
  .lock-card h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 0.3px;
  }
  .lock-card .lock-icon {
    font-size: 36px;
    line-height: 1;
  }
  #lock-password-input {
    width: 100%;
    box-sizing: border-box;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(255,255,255,0.05);
    color: var(--text);
    font-size: 15px;
    outline: none;
    transition: border-color 0.15s;
  }
  #lock-password-input:focus { border-color: var(--accent); }
  #lock-password-input.lock-error { border-color: var(--danger); }
  #lock-hint-link {
    font-size: 12px;
    color: var(--text-dim);
    cursor: pointer;
    text-decoration: underline;
    align-self: flex-start;
  }
  #lock-hint-text {
    font-size: 12px;
    color: var(--yellow);
    align-self: flex-start;
    display: none;
  }
  .lock-recovery {
    font-size: 10px;
    color: var(--text-dim);
    text-align: center;
    line-height: 1.5;
    margin-top: 6px;
  }
  @keyframes lock-shake {
    0%,100% { transform: translateX(0); }
    20%      { transform: translateX(-8px); }
    40%      { transform: translateX(8px); }
    60%      { transform: translateX(-5px); }
    80%      { transform: translateX(5px); }
  }
  .lock-shake { animation: lock-shake 0.35s ease; }
  ```

- [ ] **Step 2: Verify the CSS is syntactically valid**

  ```bash
  python3 -c "
  import re
  css = open('TelegramManager.app/Contents/Resources/index.html').read()
  style = re.search(r'<style>(.*?)</style>', css, re.DOTALL).group(1)
  print('{ count:', style.count('{'), '} count:', style.count('}'))
  "
  ```
  Expected: `{` count == `}` count.

- [ ] **Step 3: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: add lock screen CSS"
  ```

---

## Task 3: Add lock screen HTML

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Add the lock screen overlay HTML**

  Find the line `<!-- Right-click context menu -->` (around line 959). Insert the following block **immediately before** it:

  ```html
  <!-- Lock screen -->
  <div id="lock-screen">
    <div class="lock-card">
      <div class="lock-icon">🔒</div>
      <h2>TelegramManager</h2>
      <input type="password" id="lock-password-input" placeholder="Enter password…"
             autocomplete="current-password"
             onkeydown="if(event.key==='Enter') tryUnlock()">
      <span id="lock-hint-link" style="display:none" onclick="showLockHint()">Show hint</span>
      <span id="lock-hint-text"></span>
      <button class="btn primary" style="width:100%;padding:10px" onclick="tryUnlock()">Unlock</button>
      <p class="lock-recovery">Forgot your password? Edit <code>manager_config.json</code> and remove the <code>lock_password_hash</code> key.</p>
    </div>
  </div>
  ```

- [ ] **Step 2: Verify HTML structure**

  Open the app. The lock screen should not be visible yet (no password is configured). No console errors.

- [ ] **Step 3: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: add lock screen HTML overlay"
  ```

---

## Task 4: Add core lock JS (hash, lock, unlock, idle timer)

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Add the lock JS section**

  Find the comment `// ── Auto-refresh ──────────────────────────────────────────────────────────` (around line 2904). Insert the following block **immediately before** it:

  ```javascript
  // ── Password lock ─────────────────────────────────────────────────────────

  let _lockConfig = { hash: null, salt: '', hint: '', timeout: 5 };
  let _lockTimer  = null;

  async function hashPassword(salt, password) {
    const enc = new TextEncoder();
    const buf = await crypto.subtle.digest('SHA-256', enc.encode(salt + password));
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
  }

  function generateSalt() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes).map(b => b.toString(16).padStart(2,'0')).join('');
  }

  function lockApp() {
    clearTimeout(_lockTimer);
    document.getElementById('lock-screen').classList.add('visible');
    const inp = document.getElementById('lock-password-input');
    inp.value = '';
    inp.classList.remove('lock-error');
    document.getElementById('lock-hint-text').style.display = 'none';
    setTimeout(() => inp.focus(), 50);
  }

  function showLockHint() {
    const el = document.getElementById('lock-hint-text');
    el.textContent = _lockConfig.hint;
    el.style.display = 'block';
    document.getElementById('lock-hint-link').style.display = 'none';
  }

  async function tryUnlock() {
    const inp  = document.getElementById('lock-password-input');
    const hash = await hashPassword(_lockConfig.salt, inp.value);
    if (hash === _lockConfig.hash) {
      document.getElementById('lock-screen').classList.remove('visible');
      resetIdleTimer();
    } else {
      inp.classList.add('lock-error', 'lock-shake');
      inp.addEventListener('animationend', () => {
        inp.classList.remove('lock-shake');
      }, { once: true });
      inp.value = '';
      setTimeout(() => inp.focus(), 50);
    }
  }

  function resetIdleTimer() {
    clearTimeout(_lockTimer);
    if (!_lockConfig.hash) return;
    const ms = (_lockConfig.timeout || 0) * 60 * 1000;
    if (ms > 0) _lockTimer = setTimeout(lockApp, ms);
  }

  function initLock(cfg) {
    _lockConfig.hash    = cfg.lock_password_hash || null;
    _lockConfig.salt    = cfg.lock_password_salt || '';
    _lockConfig.hint    = cfg.lock_hint || '';
    _lockConfig.timeout = cfg.lock_timeout_minutes ?? 5;

    const hintLink = document.getElementById('lock-hint-link');
    if (_lockConfig.hint) hintLink.style.display = 'block';

    if (_lockConfig.hash) {
      lockApp();
      ['mousemove','keydown','mousedown','touchstart'].forEach(ev =>
        document.addEventListener(ev, resetIdleTimer, { passive: true })
      );
    }
  }
  ```

- [ ] **Step 2: Verify functions are defined without console errors**

  Open the app in a browser or WKWebView, open DevTools console, and run:
  ```javascript
  typeof hashPassword   // "function"
  typeof lockApp        // "function"
  typeof tryUnlock      // "function"
  typeof initLock       // "function"
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: add lock screen JS — hash, lock, unlock, idle timer"
  ```

---

## Task 5: Wire lock into the boot sequence

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Update `boot()` to fetch config and call `initLock()`**

  Find the boot IIFE at the bottom of the `<script>` block. It currently looks like:

  ```javascript
  (async function boot() {
    const content = document.getElementById("content");
    for (let attempt = 0; attempt < 20; attempt++) {
      const r = await api("/api/accounts");
      if (r && !r._networkError && Array.isArray(r)) {
        accounts = r;
        renderAccounts();
        loadAlerts();
        loadWorkspaceBar();
        api("/api/stats").then(s => { if (s) { _statsData = s; updateStats(); } });
        return;
      }
  ```

  Add one line after the `api("/api/stats")` call:

  ```javascript
  (async function boot() {
    const content = document.getElementById("content");
    for (let attempt = 0; attempt < 20; attempt++) {
      const r = await api("/api/accounts");
      if (r && !r._networkError && Array.isArray(r)) {
        accounts = r;
        renderAccounts();
        loadAlerts();
        loadWorkspaceBar();
        api("/api/stats").then(s => { if (s) { _statsData = s; updateStats(); } });
        api("/api/config").then(cfg => { if (cfg) initLock(cfg); });
        return;
      }
  ```

- [ ] **Step 2: Test lock-on-launch manually**

  Set a temporary hash directly in `manager_config.json` to verify the lock shows:
  ```bash
  python3 -c "
  import json, hashlib
  salt = 'aabbccddeeff00112233445566778899'
  pw   = 'test'
  h    = hashlib.sha256((salt + pw).encode()).hexdigest()
  cfg  = json.load(open('data/manager_config.json'))
  cfg['lock_password_hash'] = h
  cfg['lock_password_salt'] = salt
  cfg['lock_hint']          = 'my test hint'
  cfg['lock_timeout_minutes'] = 1
  json.dump(cfg, open('data/manager_config.json','w'), indent=2)
  print('hash:', h)
  "
  ```

  Open the app — lock screen should appear immediately. Type `test` and press Enter — it should unlock. Wait 1 minute idle — it should lock again. Click "Show hint" — should show "my test hint".

- [ ] **Step 3: Remove the test hash from config**

  ```bash
  python3 -c "
  import json
  cfg = json.load(open('data/manager_config.json'))
  cfg['lock_password_hash'] = None
  cfg['lock_password_salt'] = None
  cfg['lock_hint'] = ''
  json.dump(cfg, open('data/manager_config.json','w'), indent=2)
  print('Lock cleared')
  "
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: wire initLock() into boot sequence"
  ```

---

## Task 6: Add Security tab HTML to Settings modal

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Add the "Security" tab button**

  Find the tab bar inside the Settings modal (around line 1203):
  ```html
      <div class="settings-tabs">
        <button class="settings-tab active" onclick="switchSettingsTab('general',this)">General</button>
        <button class="settings-tab" onclick="switchSettingsTab('keeper',this)">Keeper</button>
        <button class="settings-tab" onclick="switchSettingsTab('advanced',this)">Advanced</button>
      </div>
  ```

  Replace with:
  ```html
      <div class="settings-tabs">
        <button class="settings-tab active" onclick="switchSettingsTab('general',this)">General</button>
        <button class="settings-tab" onclick="switchSettingsTab('keeper',this)">Keeper</button>
        <button class="settings-tab" onclick="switchSettingsTab('advanced',this)">Advanced</button>
        <button class="settings-tab" onclick="switchSettingsTab('security',this)">Security</button>
      </div>
  ```

- [ ] **Step 2: Add the Security tab panel**

  Find the closing `</div>` of the Advanced panel followed by the modal actions (around line 1268):
  ```html
      </div>

      <div class="modal-actions" style="margin-top:20px">
  ```

  Insert the Security panel between them:
  ```html
      </div>

      <!-- Tab: Security -->
      <div class="settings-panel" id="stab-security">
        <p class="settings-section-title">Password Lock</p>
        <div id="sec-status-line" style="font-size:13px;margin-bottom:14px;color:var(--text-dim)">Loading…</div>
        <div id="sec-action-btns" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px"></div>

        <!-- Inline password form (hidden by default) -->
        <div id="sec-form" style="display:none">
          <div id="sec-current-wrap" style="margin-bottom:10px">
            <label>Current password:</label>
            <input type="password" id="sec-current-pw" placeholder="Current password" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text);font-size:13px;outline:none;box-sizing:border-box">
          </div>
          <div style="margin-bottom:10px">
            <label>New password:</label>
            <input type="password" id="sec-new-pw" placeholder="New password" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text);font-size:13px;outline:none;box-sizing:border-box">
          </div>
          <div style="margin-bottom:10px">
            <label>Confirm new password:</label>
            <input type="password" id="sec-confirm-pw" placeholder="Confirm new password" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text);font-size:13px;outline:none;box-sizing:border-box">
          </div>
          <div style="margin-bottom:14px">
            <label>Hint (optional — shown on lock screen):</label>
            <input type="text" id="sec-hint" placeholder="e.g. My cat's name" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text);font-size:13px;outline:none;box-sizing:border-box">
          </div>
          <div id="sec-form-error" style="font-size:12px;color:var(--danger);margin-bottom:8px;display:none"></div>
          <div style="display:flex;gap:8px">
            <button class="btn" onclick="hideSecurityForm()">Cancel</button>
            <button class="btn primary" onclick="savePasswordForm()">Save Password</button>
          </div>
        </div>

        <p class="settings-section-title" style="margin-top:20px">Auto-lock Timeout</p>
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
          <input type="number" id="cfg-lock-timeout" min="0" max="1440" value="5"
                 style="width:70px;padding:7px 10px;border:1px solid var(--border);border-radius:8px;background:rgba(255,255,255,0.05);color:var(--text);font-size:13px;outline:none">
          <span style="font-size:13px">minutes &nbsp;<span style="color:var(--text-dim);font-size:11px">(0 = never auto-lock)</span></span>
        </div>
      </div>

      <div class="modal-actions" style="margin-top:20px">
  ```

- [ ] **Step 3: Verify the new tab appears in Settings**

  Open Settings → click "Security" tab. Panel should be visible (with "Loading…" status and the timeout input). No JS errors.

- [ ] **Step 4: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: add Security tab HTML to Settings modal"
  ```

---

## Task 7: Add Security tab JS + wire showSettings/saveSettings

**Files:**
- Modify: `TelegramManager.app/Contents/Resources/index.html`

- [ ] **Step 1: Add the Security tab JS functions**

  Find the `initLock` function added in Task 4. Append the following block **immediately after** `initLock` closes (before the auto-refresh section):

  ```javascript
  // ── Security settings tab ─────────────────────────────────────────────────

  let _secMode = 'set'; // 'set' or 'change'

  function renderSecurityStatus(cfg) {
    const hasPassword = !!cfg.lock_password_hash;
    document.getElementById('sec-status-line').textContent = hasPassword
      ? '🔒 Password protection is enabled.'
      : '🔓 No password set — app is unlocked.';

    const btns = document.getElementById('sec-action-btns');
    btns.replaceChildren();   // clear previous buttons (DOM-safe, no innerHTML)
    if (!hasPassword) {
      const b = document.createElement('button');
      b.className = 'btn primary';
      b.style.fontSize = '12px';
      b.textContent = 'Set Password';
      b.onclick = () => showSecurityForm('set');
      btns.appendChild(b);
    } else {
      const ch = document.createElement('button');
      ch.className = 'btn';
      ch.style.fontSize = '12px';
      ch.textContent = 'Change Password';
      ch.onclick = () => showSecurityForm('change');
      btns.appendChild(ch);

      const rm = document.createElement('button');
      rm.className = 'btn danger';
      rm.style.fontSize = '12px';
      rm.textContent = 'Remove Password';
      rm.onclick = removePassword;
      btns.appendChild(rm);
    }
  }

  function showSecurityForm(mode) {
    _secMode = mode;
    document.getElementById('sec-current-wrap').style.display = mode === 'change' ? 'block' : 'none';
    document.getElementById('sec-current-pw').value = '';
    document.getElementById('sec-new-pw').value = '';
    document.getElementById('sec-confirm-pw').value = '';
    document.getElementById('sec-hint').value = _lockConfig.hint || '';
    document.getElementById('sec-form-error').style.display = 'none';
    document.getElementById('sec-form').style.display = 'block';
  }

  function hideSecurityForm() {
    document.getElementById('sec-form').style.display = 'none';
  }

  async function savePasswordForm() {
    const errEl   = document.getElementById('sec-form-error');
    const newPw   = document.getElementById('sec-new-pw').value;
    const confirm = document.getElementById('sec-confirm-pw').value;
    const hint    = document.getElementById('sec-hint').value.trim();

    errEl.style.display = 'none';

    if (!newPw) {
      errEl.textContent = 'New password cannot be empty.';
      errEl.style.display = 'block';
      return;
    }
    if (newPw !== confirm) {
      errEl.textContent = 'Passwords do not match.';
      errEl.style.display = 'block';
      return;
    }
    if (_secMode === 'change') {
      const currentPw   = document.getElementById('sec-current-pw').value;
      const currentHash = await hashPassword(_lockConfig.salt, currentPw);
      if (currentHash !== _lockConfig.hash) {
        errEl.textContent = 'Current password is incorrect.';
        errEl.style.display = 'block';
        return;
      }
    }

    const salt    = generateSalt();
    const hash    = await hashPassword(salt, newPw);
    const timeout = parseInt(document.getElementById('cfg-lock-timeout').value) || 0;

    const r = await api('/api/config', 'POST', {
      lock_password_hash:   hash,
      lock_password_salt:   salt,
      lock_hint:            hint,
      lock_timeout_minutes: timeout,
    });

    if (r && r.success !== false) {
      _lockConfig.hash    = hash;
      _lockConfig.salt    = salt;
      _lockConfig.hint    = hint;
      _lockConfig.timeout = timeout;

      // Ensure idle listeners are registered now that a password exists
      ['mousemove','keydown','mousedown','touchstart'].forEach(ev =>
        document.removeEventListener(ev, resetIdleTimer)
      );
      ['mousemove','keydown','mousedown','touchstart'].forEach(ev =>
        document.addEventListener(ev, resetIdleTimer, { passive: true })
      );
      resetIdleTimer();

      hideSecurityForm();
      api('/api/config').then(cfg => { if (cfg) renderSecurityStatus(cfg); });
      toast('Password saved ✓', 'success');
    } else {
      errEl.textContent = r?.message || 'Failed to save password.';
      errEl.style.display = 'block';
    }
  }

  async function removePassword() {
    if (!confirm('Remove password protection? The app will be unlocked on next launch.')) return;
    const timeout = parseInt(document.getElementById('cfg-lock-timeout').value) || 0;
    const r = await api('/api/config', 'POST', {
      lock_password_hash:   null,
      lock_password_salt:   null,
      lock_hint:            '',
      lock_timeout_minutes: timeout,
    });
    if (r && r.success !== false) {
      _lockConfig.hash = null;
      _lockConfig.salt = '';
      _lockConfig.hint = '';
      clearTimeout(_lockTimer);
      ['mousemove','keydown','mousedown','touchstart'].forEach(ev =>
        document.removeEventListener(ev, resetIdleTimer)
      );
      api('/api/config').then(cfg => { if (cfg) renderSecurityStatus(cfg); });
      toast('Password removed', 'info');
    } else {
      toast(r?.message || 'Failed to remove password', 'error');
    }
  }
  ```

- [ ] **Step 2: Update `showSettings()` to load the Security tab state**

  Find `showSettings()` (around line 2565). It currently ends with:
  ```javascript
    document.getElementById("settings-modal").classList.add("active");
    loadKeeperStatus();
    loadSharedAppStatus();
  });
  ```

  Replace that closing section with:
  ```javascript
    document.getElementById("cfg-lock-timeout").value = cfg.lock_timeout_minutes ?? 5;
    document.getElementById("sec-status-line").textContent = "Loading…";
    hideSecurityForm();
    renderSecurityStatus(cfg);
    document.getElementById("settings-modal").classList.add("active");
    loadKeeperStatus();
    loadSharedAppStatus();
  });
  ```

- [ ] **Step 3: Update `saveSettings()` to include `lock_timeout_minutes`**

  Find `saveSettings()` (around line 2594). The `api("/api/config", "POST", {...})` call currently sends:
  ```javascript
  const r = await api("/api/config", "POST", {
    app_source, extra_scan_dirs,
    keeper_enabled, keeper_interval_days, keeper_open_seconds,
    auto_clear_cache_mb
  });
  ```

  Replace with:
  ```javascript
  const lock_timeout_minutes = parseInt(document.getElementById("cfg-lock-timeout").value) || 0;
  const r = await api("/api/config", "POST", {
    app_source, extra_scan_dirs,
    keeper_enabled, keeper_interval_days, keeper_open_seconds,
    auto_clear_cache_mb,
    lock_timeout_minutes,
  });
  ```

  Then find `hideSettings();` immediately after that call and add two lines after it:
  ```javascript
  hideSettings();
  _lockConfig.timeout = lock_timeout_minutes;
  resetIdleTimer();
  ```

- [ ] **Step 4: End-to-end test**

  1. Open Settings → Security tab — shows "🔓 No password set" + "Set Password" button
  2. Click "Set Password" — inline form appears (no "Current password" field)
  3. Enter new password `hello123`, hint `greeting word`, click "Save Password"
  4. Toast shows "Password saved ✓". Status shows "🔒 Password protection is enabled."
  5. Close and reopen Settings → Security — shows "Change Password" and "Remove Password"
  6. Close the app and reopen — lock screen appears immediately
  7. Type wrong password — input shakes and clears
  8. Type `hello123` — unlocks
  9. Click "Show hint" on lock screen — shows "greeting word"
  10. Set timeout to 1 minute in Security tab, save. Wait 1 minute idle — app locks
  11. Settings → Security → "Remove Password" → confirm — status shows "🔓 No password set"
  12. Relaunch app — no lock screen

- [ ] **Step 5: Commit**

  ```bash
  git add TelegramManager.app/Contents/Resources/index.html
  git commit -m "feat: Security settings tab — set, change, remove password + idle timeout config"
  ```

---

## Self-Review

**Spec coverage check:**
- ✅ Lock on every launch → `initLock()` called from `boot()`
- ✅ Auto-lock after idle → `resetIdleTimer()` + event listeners on `mousemove`, `keydown`, `mousedown`, `touchstart`
- ✅ Configurable timeout → `cfg-lock-timeout` input, saved via `saveSettings()` + `savePasswordForm()`
- ✅ UI-only lock, Telegram keeps running → overlay only, no account close calls
- ✅ Security tab in Settings (4th tab) → Task 6 + Task 7
- ✅ Set / Change / Remove password → `showSecurityForm()`, `savePasswordForm()`, `removePassword()`
- ✅ Password hint → `sec-hint` input stored in `lock_hint`; shown on lock screen via `showLockHint()`
- ✅ SHA-256 + random salt → `hashPassword()` uses `crypto.subtle`, `generateSalt()` uses `crypto.getRandomValues`
- ✅ Recovery note on lock screen → `<p class="lock-recovery">` in Task 3
- ✅ Server: 4 new config keys in `DEFAULT_CONFIG` + allowlist → Task 1
- ✅ No innerHTML with dynamic content → `replaceChildren()` + `textContent` used throughout
