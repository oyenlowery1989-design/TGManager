"""Open accounts in AyuGram using a persistent, isolated copy of tdata."""

import hashlib
import json
import os
import plistlib
import shutil
import stat
import struct
import subprocess
import time
import tempfile

import state
import updates

BUNDLE_ID = 'one.ayugram.AyuGramDesktop'
INLINE_BUNDLE_ID = 'one.telegrammanager.AyuGramInlineTest'
INLINE_EXECUTABLE = 'AyuGram Inline Test'
_BUNDLE_EXECUTABLES = {BUNDLE_ID: 'AyuGram', INLINE_BUNDLE_ID: INLINE_EXECUTABLE}
_INLINE_MARKER = b'ayugram-inline-isolated-profile-v1\n'
_NATIVE_SETTINGS_FILES = ('settingss', 'settings0', 'settings1')

# Preferences only; never copy encrypted settings or authentication files.
COMMON_PROFILE = {
    'saveDeletedMessages': True,
    'saveMessagesHistory': True,
    'useGlobalGhostMode': True,
    'ghostModeSettings': {'0': {
        'sendReadMessages': False,
        'sendReadStories': False,
        'sendOnlinePackets': False,
        'sendUploadProgress': False,
        'sendOfflinePacketAfterOnline': True,
        'markReadAfterAction': False,
    }},
    'translationProvider': 'telegram',
    'showPeerId': 2,
}


def _apply_common_profile(account_path, inline_history=False):
    """Apply once per profile revision, only inside the isolated AyuGram copy."""
    runtime = runtime_dir(account_path)
    tdata = os.path.join(runtime, 'TelegramForcePortable', 'tdata')
    settings_path = os.path.join(tdata, 'ayu_settings.json')
    marker = os.path.join(runtime, '.common-profile.json')
    if not state.is_managed_account_path(account_path):
        raise ValueError('Path is not a valid account folder')
    for path in (runtime, os.path.dirname(tdata), tdata, settings_path,
                 settings_path + '.tmp', marker, marker + '.tmp'):
        if (os.path.islink(path)
                or not os.path.realpath(path).startswith(os.path.realpath(account_path) + os.sep)
                or (os.path.isfile(path) and os.stat(path).st_nlink > 1)):
            raise ValueError('AyuGram profile contains an unsafe path')
    fingerprint = hashlib.sha256(json.dumps(COMMON_PROFILE, sort_keys=True).encode()).hexdigest()
    already_applied = False
    applied = {}
    if os.path.exists(marker):
        with open(marker, encoding='utf-8') as f:
            applied = json.load(f)
        if not isinstance(applied, dict):
            raise ValueError('Invalid AyuGram profile marker')
        already_applied = applied.get('fingerprint') == fingerprint
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, encoding='utf-8') as f:
            settings = json.load(f)
    if not isinstance(settings, dict):
        raise ValueError('Invalid AyuGram settings; existing file was preserved')
    changed = False
    if already_applied:
        ghosts = settings.get('ghostModeSettings')
        # AyuGram's first unauthenticated startup can save an empty Ghost map.
        if settings.get('useGlobalGhostMode') is True and isinstance(ghosts, dict) and '0' not in ghosts:
            ghosts['0'] = dict(COMMON_PROFILE['ghostModeSettings']['0'])
            changed = True
    else:
        ghosts = settings.setdefault('ghostModeSettings', {})
        if not isinstance(ghosts, dict) or not isinstance(ghosts.setdefault('0', {}), dict):
            raise ValueError('Invalid AyuGram Ghost Mode settings; existing file was preserved')
        ghosts['0'].update(COMMON_PROFILE['ghostModeSettings']['0'])
        settings.update({key: value for key, value in COMMON_PROFILE.items() if key != 'ghostModeSettings'})
        applied['fingerprint'] = fingerprint
        changed = True
    if inline_history and applied.get('inline_history') != 1:
        settings['semiTransparentDeletedMessages'] = True
        applied['inline_history'] = 1
        changed = True
    if not changed:
        return
    state._save_json_atomic(settings_path, settings)
    state._save_json_atomic(marker, applied)


def runtime_dir(account_path):
    return os.path.join(account_path, '.ayugram')


def _bundle_executable(app):
    """Only approved client identities may supply an executable name."""
    path = os.path.join(app, 'Contents', 'Info.plist')
    if os.path.islink(path):
        raise ValueError('Unsafe AyuGram Info.plist path')
    with open(path, 'rb') as f:
        info = plistlib.load(f)
    if not isinstance(info, dict):
        raise ValueError('Invalid AyuGram bundle metadata')
    name = _BUNDLE_EXECUTABLES.get(info.get('CFBundleIdentifier'))
    if not name or info.get('CFBundleExecutable', name) != name:
        raise ValueError('Unsupported AyuGram bundle identity or executable')
    return os.path.join(app, 'Contents', 'MacOS', name)


def _trusted_master(app):
    executable_path = _bundle_executable(app)
    if not os.path.isfile(executable_path) or os.path.islink(executable_path):
        raise ValueError('The shared AyuGram executable is missing or unsafe')
    inline = os.path.basename(executable_path) == INLINE_EXECUTABLE
    fingerprint = bundle_fingerprint(app)
    with open(os.path.join(os.path.dirname(app), '.ayugram-official.json'), encoding='utf-8') as f:
        trusted = json.load(f)
    if (not isinstance(trusted, dict) or trusted.get('fingerprint') != fingerprint
            or trusted.get('kind', 'official') != ('local-inline' if inline else 'official')):
        raise ValueError('The shared AyuGram app does not match its verified installation')
    return fingerprint, inline


def _prepare_inline_profile(account_path):
    """Authorize only the existing isolated copy, never the Telegram workdir."""
    if not state.is_managed_account_path(account_path):
        raise ValueError('Path is not a valid account folder')
    portable = os.path.join(runtime_dir(account_path), 'TelegramForcePortable')
    if (os.path.islink(runtime_dir(account_path)) or os.path.islink(portable)
            or not os.path.realpath(portable).startswith(os.path.realpath(account_path) + os.sep)
            or not os.path.isdir(portable)):
        raise ValueError('Inline history requires an isolated AyuGram profile')
    portable = os.path.realpath(portable)
    for root, dirs, files in os.walk(portable):
        for name in dirs + files:
            info = os.lstat(os.path.join(root, name))
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise ValueError('Inline AyuGram profile contains linked or unsafe files')
    marker = os.path.join(portable, '.ayugram-inline-test')
    if os.path.exists(marker):
        with open(marker, 'rb') as f:
            if f.read() != _INLINE_MARKER:
                raise ValueError('Invalid inline AyuGram profile marker; existing files were preserved')
    else:
        with open(marker, 'xb') as f:
            f.write(_INLINE_MARKER)
        os.chmod(marker, 0o600)
    return ['-workdir', portable]


def _workdir_args(account_path, app):
    return (_prepare_inline_profile(account_path)
            if os.path.basename(_bundle_executable(app)) == INLINE_EXECUTABLE else [])


def find_app(account_path):
    """Resolve one isolated clone; ambiguous or redirected bundles are unsafe."""
    runtime = runtime_dir(account_path)
    if (os.path.islink(runtime)
            or not os.path.realpath(runtime).startswith(os.path.realpath(account_path) + os.sep)):
        raise ValueError('AyuGram copy contains an unsafe path')
    if not os.path.exists(runtime):
        return None
    apps = []
    for name in os.listdir(runtime):
        if not name.endswith('.app'):
            continue
        app = os.path.join(runtime, name)
        exe = _bundle_executable(app)
        for path in (app, os.path.dirname(os.path.dirname(exe)), os.path.dirname(exe), exe):
            if (os.path.islink(path)
                    or not os.path.realpath(path).startswith(os.path.realpath(runtime) + os.sep)):
                raise ValueError('AyuGram app contains an unsafe path')
        if not os.path.isdir(app) or not os.path.isfile(exe):
            raise ValueError('AyuGram app is not a valid clone')
        apps.append(app)
    if len(apps) > 1:
        raise ValueError('Multiple AyuGram app clones found; cannot identify the running app safely')
    return apps[0] if apps else None


def executable(account_path):
    try:
        app = find_app(account_path)
        if app:
            return _bundle_executable(app)
    except (OSError, ValueError):
        return ''
    return os.path.join(runtime_dir(account_path), 'AyuGram.app', 'Contents', 'MacOS', 'AyuGram')


def executables(account_path):
    """Process detection must include ambiguous clones even when launch refuses them."""
    runtime = runtime_dir(account_path)
    try:
        paths = [os.path.join(runtime, name, 'Contents', 'MacOS', exe)
                 for name in sorted(os.listdir(runtime)) if name.endswith('.app')
                 for exe in _BUNDLE_EXECUTABLES.values()]
    except FileNotFoundError:
        paths = []
    except OSError:
        return []
    return paths or [os.path.join(runtime, 'AyuGram.app', 'Contents', 'MacOS', 'AyuGram')]


def session_version(tdata):
    """Older clients discard key files with a newer TDF version header."""
    versions = []
    for name in ('key_datas', 'key_data0', 'key_data1'):
        path = os.path.join(tdata, name)
        try:
            with open(path, 'rb') as f:
                header = f.read(8)
        except FileNotFoundError:
            continue
        if len(header) == 8 and header[:4] == b'TDF$':
            version = struct.unpack('<i', header[4:])[0]
            if version > 0:
                versions.append(version)
    if not versions:
        return None
    version = max(versions)
    return f'{version // 1000000}.{version // 1000 % 1000}.{version % 1000}'


def bundle_fingerprint(app, ignore_display_name=False):
    """Pin every file in the official unsigned macOS release, including libraries."""
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(app):
        dirs.sort()
        for name in sorted(dirs + files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, app).encode() + b'\0')
            if os.path.islink(path):
                digest.update(b'link:' + os.readlink(path).encode())
            elif os.path.isfile(path):
                if ignore_display_name and os.path.relpath(path, app) == 'Contents/Info.plist':
                    with open(path, 'rb') as f:
                        info = plistlib.load(f)
                    for key in ('CFBundleName', 'CFBundleDisplayName'):
                        info.pop(key, None)
                    digest.update(plistlib.dumps(info, sort_keys=True))
                    continue
                with open(path, 'rb') as f:
                    for block in iter(lambda: f.read(1024 * 1024), b''):
                        digest.update(block)
    return digest.hexdigest()


def _apply_display_name(app, display_name):
    """Edit clone metadata only; never re-sign or modify the executable."""
    path = os.path.join(app, 'Contents', 'Info.plist')
    if os.path.islink(path) or not os.path.realpath(path).startswith(os.path.realpath(app) + os.sep):
        raise ValueError('Unsafe AyuGram Info.plist path')
    with open(path, 'rb') as f:
        info = plistlib.load(f)
    if all(info.get(key) == display_name for key in ('CFBundleName', 'CFBundleDisplayName')):
        return
    info.update(CFBundleName=display_name, CFBundleDisplayName=display_name)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as f:
            temporary = f.name
            plistlib.dump(info, f)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def remove_clone_if_idle(account_path):
    """Remove only an idle app when its verified master can safely recreate it."""
    import server
    lock = state._account_path_lock(account_path)
    if not lock.acquire(blocking=False):
        return False
    try:
        if not state.is_managed_account_path(account_path):
            return False
        app = find_app(account_path)
        if not app:
            return False
        output = server._get_ps_output(force=True, allow_stale=False)
        if output is None or server._client_pid_from_ps_output(account_path, output, 'AyuGram'):
            return False
        master = os.path.join(state.DATA_DIR, '_apps', 'macOS', 'AyuGram.app')
        _trusted_master(master)
        runtime = runtime_dir(account_path)
        if server._newer_account_version(runtime, master, app):
            return False
        stamp = server._clone_stamp_path(runtime)
        if os.path.islink(stamp):
            return False
        return server._remove_account_clone(runtime, app)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        state._log.warning('Could not remove idle AyuGram clone for %s: %s', account_path, e)
        return False
    finally:
        lock.release()


def _initialize_native_settings(account_path, app):
    """Initialize an empty manager login with normal Quit and a recoverable preference seed."""
    import server
    runtime = runtime_dir(account_path)
    tdata = os.path.join(runtime, 'TelegramForcePortable', 'tdata')
    login_marker = os.path.join(runtime, '.new-login.json')
    with open(login_marker, encoding='utf-8') as f:
        if json.load(f) != {'new_login': True}:
            return False, 'AyuGram initialization requires an empty manager-created login profile'
    marker = os.path.join(runtime, '.common-profile.json')
    with open(marker, encoding='utf-8') as f:
        applied = json.load(f)
    pending = applied.get('native_initialization')
    native_exists = any(os.path.lexists(os.path.join(tdata, name)) for name in _NATIVE_SETTINGS_FILES)
    keys_exist = any(os.path.lexists(os.path.join(tdata, name)) for name in ('key_datas', 'key_data0', 'key_data1'))
    if pending is None and (native_exists or keys_exist):
        return False, 'Existing AyuGram session or settings found; initialization was skipped safely'
    output = server._get_ps_output(force=True, allow_stale=False)
    if output is None or server._client_pid_from_ps_output(account_path, output, 'AyuGram'):
        return False, 'Could not verify that the new AyuGram profile is closed'
    settings_path = os.path.join(tdata, 'ayu_settings.json')
    if pending is None:
        with open(settings_path, encoding='utf-8') as f:
            preferences = json.load(f)
        applied['native_initialization'] = {'preferences': preferences}
        state._save_json_atomic(marker, applied)
    elif isinstance(pending, dict) and isinstance(pending.get('preferences'), dict):
        preferences = pending['preferences']
    else:
        return False, 'Invalid AyuGram initialization record; existing files were preserved'
    if not native_exists:
        if keys_exist:
            return False, 'AyuGram native settings are missing; the existing session was preserved'
        # 7.2.9 ignores Ayu settings and -ghost until native core settings exist.
        result = subprocess.run(['open', '-g', '-j', '-n', app, '--args', '-noupdate', '-ghost']
                                + _workdir_args(account_path, app),
                                capture_output=True, timeout=10)
        if result.returncode:
            return False, 'Could not initialize the new AyuGram login; its files were preserved'
        pid = None
        for _ in range(20):
            output = server._get_ps_output(force=True, allow_stale=False)
            if output is None:
                return False, 'Could not check AyuGram initialization; close its hidden window before retrying'
            pid = server._client_pid_from_ps_output(account_path, output, 'AyuGram')
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            return False, 'AyuGram initialization did not start; the profile was preserved'
        server._request_client_quit(pid, account_path, 'AyuGram')
        for _ in range(20):
            output = server._get_ps_output(force=True, allow_stale=False)
            if output is None:
                return False, 'Could not confirm AyuGram initialization finished; close it before retrying'
            if not server._client_pid_from_ps_output(account_path, output, 'AyuGram'):
                break
            time.sleep(0.5)
        else:
            return False, 'AyuGram initialization did not quit normally; close it before retrying'
    initialized = False
    for name in _NATIVE_SETTINGS_FILES:
        path = os.path.join(tdata, name)
        if not os.path.lexists(path):
            continue
        if os.path.islink(path) or not os.path.isfile(path) or os.stat(path).st_nlink != 1:
            return False, 'AyuGram created unsafe native settings; its files were preserved'
        with open(path, 'rb') as f:
            header = f.read(8)
        initialized = initialized or (len(header) == 8 and header[:4] == b'TDF$'
                                      and struct.unpack('<i', header[4:])[0] > 0
                                      and os.path.getsize(path) > 24)
    if not initialized:
        return False, 'AyuGram did not create native settings; the new login was not opened'
    state._save_json_atomic(settings_path, preferences)
    applied.pop('native_initialization', None)
    state._save_json_atomic(marker, applied)
    return True, ''


@updates.guard_client_launch
@state.serialize_account_op(lambda account_path, new_login=False: account_path, (False, state._BUSY_MSG))
def open_account(account_path, new_login=False):
    import server
    from backups import _copy_tdata_excluding_cache, recover_interrupted_restore
    if not recover_interrupted_restore(account_path, client="AyuGram"):
        return False, "Could not recover an interrupted AyuGram restore; original data was preserved"
    if not state.is_managed_account_path(account_path):
        return False, 'Path is not a valid account folder'
    try:
        app = find_app(account_path) or os.path.join(runtime_dir(account_path), 'AyuGram.app')
    except (OSError, ValueError) as e:
        return False, f'Could not open AyuGram: {e}'
    output = server._get_ps_output(force=True, allow_stale=False)
    if output is None:
        return False, 'Could not check running apps — try again'
    if server._client_pid_from_ps_output(account_path, output, "AyuGram"):
        return False, 'AyuGram is already running for this account'

    runtime = runtime_dir(account_path)
    portable = os.path.join(runtime, 'TelegramForcePortable')
    tdata = os.path.join(portable, 'tdata')
    staging = tdata + '.partial'
    stamp = server._clone_stamp_path(runtime)
    login_marker = os.path.join(runtime, '.new-login.json')
    for path in (runtime, portable, tdata, staging, app, stamp, stamp + '.tmp',
                 login_marker, login_marker + '.tmp'):
        if os.path.islink(path) or not os.path.realpath(path).startswith(os.path.realpath(account_path) + os.sep):
            return False, 'AyuGram copy contains an unsafe path'

    try:
        updates.recover_ayugram_update()
    except (OSError, ValueError) as e:
        return False, f'Could not recover the AyuGram app update: {e}'
    master = os.path.join(state.DATA_DIR, '_apps', 'macOS', 'AyuGram.app')
    if not os.path.isdir(master):
        return False, 'AyuGram is not set up. Install a verified AyuGram shared app first'
    try:
        fingerprint, inline = _trusted_master(master)
        newer = server._newer_account_version(runtime, master, app)
        if newer:
            return False, f'Update the shared AyuGram app to {newer} or newer first'
        source = os.path.join(account_path, 'TelegramForcePortable', 'tdata')
        fresh_profile = os.path.isfile(login_marker)
        if fresh_profile:
            with open(login_marker, encoding='utf-8') as f:
                if json.load(f) != {'new_login': True}:
                    return False, 'Invalid AyuGram login profile marker'
        if new_login and os.path.exists(tdata) and not fresh_profile:
            return False, 'An AyuGram session copy already exists; it will not be reset'
        empty_login = (fresh_profile or new_login) and not any(
            os.path.lexists(os.path.join(tdata, name)) for name in ('key_datas', 'key_data0', 'key_data1'))
        if not new_login and not fresh_profile and not os.path.isdir(tdata) and server.find_client_pid(account_path, 'Telegram', fresh=True):
            return False, 'Close Telegram before importing its session, or use a new AyuGram login'
        data_version = None if empty_login else session_version(tdata if os.path.isdir(tdata) else source)
        if data_version is None and not empty_login:
            return False, 'No readable session key found — refusing to open an empty AyuGram login'
        master_version = server._version_tuple(server._bundle_version(master))
        if master_version is None or (data_version and server._version_tuple(data_version) > master_version):
            return False, (f'This session was written by Telegram {data_version}. '
                           f'AyuGram {server._bundle_version(master)} cannot import it safely. '
                           'A newer compatible AyuGram release is required; the original session was not changed.')
        os.makedirs(portable, mode=0o700, exist_ok=True)
        os.chmod(runtime, 0o700)
        if (new_login or fresh_profile) and not os.path.exists(tdata):
            state._save_json_atomic(login_marker, {'new_login': True})
            os.makedirs(tdata, mode=0o700)
        if not os.path.isdir(tdata):
            for root, dirs, files in os.walk(source):
                if any(os.path.islink(os.path.join(root, name)) for name in dirs + files):
                    return False, 'Session contains symbolic links — cannot make an isolated AyuGram copy'
            if os.path.exists(staging):
                shutil.rmtree(staging)
            ok, error = _copy_tdata_excluding_cache(source, staging)
            if not ok:
                if os.path.isdir(staging):
                    shutil.rmtree(staging)
                return False, f'Could not copy session for AyuGram: {error}'
            os.rename(staging, tdata)
            state._log.info('Created isolated AyuGram session copy for %s', account_path)

        if (not os.path.isdir(app) or server._read_clone_stamp(runtime) != fingerprint
                or bundle_fingerprint(app, ignore_display_name=not inline)
                   != bundle_fingerprint(master, ignore_display_name=not inline)):
            ok, error = server._copy_app_bundle(master, app)
            if not ok:
                return False, f'Could not copy AyuGram app: {error}'
            server._write_clone_stamp(runtime, fingerprint)
        with state._meta_lock:
            display_name = state.metadata.get('dock_names', {}).get(account_path) or os.path.basename(account_path)
        # The local build seals Info.plist; its bundle basename supplies the Dock name.
        if not inline:
            _apply_display_name(app, display_name)
        named_app = os.path.join(runtime, server._safe_bundle_name(display_name, os.path.basename(account_path)) + '.app')
        if app != named_app:
            if os.path.lexists(named_app):
                return False, 'The AyuGram Dock name destination already exists; existing files were preserved'
            # Telegram-derived clients use the bundle basename for their Dock label.
            os.rename(app, named_app)
            app = named_app
        ok, error = server._disable_telegram_auto_updates(runtime, app, client='AyuGram')
        if not ok:
            return False, error
        workdir_args = _workdir_args(account_path, app)
        _apply_common_profile(account_path, inline_history=inline)
        with open(os.path.join(runtime, '.common-profile.json'), encoding='utf-8') as f:
            pending_initialization = 'native_initialization' in json.load(f)
        if pending_initialization or (empty_login and not any(
                os.path.lexists(os.path.join(tdata, name)) for name in _NATIVE_SETTINGS_FILES)):
            ok, error = _initialize_native_settings(account_path, app)
            if not ok:
                return False, error
        with open(os.path.join(tdata, 'ayu_settings.json'), encoding='utf-8') as f:
            preferences = json.load(f)
        ghost = preferences.get('ghostModeSettings', {}).get('0', {})
        # AyuGram can rewrite Ghost preferences during its first startup.
        ghost_args = ['-ghost'] if preferences.get('useGlobalGhostMode') and all(
            ghost.get(key) is False for key in ('sendReadMessages', 'sendReadStories',
                                               'sendOnlinePackets', 'sendUploadProgress')) else []
        result = subprocess.run(['open', '-n', app, '--args', '-noupdate'] + ghost_args + workdir_args,
                                capture_output=True, timeout=10)
        if result.returncode:
            return False, 'Could not launch AyuGram: ' + result.stderr.decode(errors='replace').strip()
        for _ in range(20):
            output = server._get_ps_output(force=True, allow_stale=False)
            if output and server._client_pid_from_ps_output(account_path, output, "AyuGram"):
                server.invalidate_scan_cache()
                return True, ('AyuGram opened using its separate login profile' if fresh_profile or new_login
                              else 'AyuGram opened using its separate session copy')
            time.sleep(0.5)
        return False, 'AyuGram did not start within 10 seconds'
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        state._log.warning('Could not open isolated AyuGram for %s: %s', account_path, e)
        return False, f'Could not open AyuGram: {e}'
