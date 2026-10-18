#!/usr/bin/python3
"""Fixed-path remote runtime: no session import or arbitrary command execution."""
import fcntl
import hashlib
import importlib.util
import json
import os
import pwd
import re
from pathlib import Path
import stat
import sqlite3
import struct
import subprocess
import sys

COMMIT = 'd4b7546e77f93ac4c9bb53a3fcf7e9ffbbecf1b5'
NAME = 'AyuGram Inline Test'
MARKER = b'ayugram-inline-isolated-profile-v1\n'
BASE = Path('/opt/ayugram-remote')
PROFILE = Path('/var/lib/ayugram-remote/profile')
UNITS = ('ayugram-remote-client.service', 'ayugram-remote-display.service',
         'ayugram-remote-desktop.service', 'ayugram-remote-vnc.service')
UNIT_STATES = ('active', 'reloading', 'inactive', 'failed', 'activating',
               'deactivating', 'maintenance', 'refreshing')


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Refusing a linked or non-regular file')


def digest(path):
    regular(path)
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def verify_release(directory, expected_sha, expected_patch):
    """An operator-provided digest is the trust anchor; manifest is a receipt."""
    regular(directory / 'release.json')
    manifest = json.loads((directory / 'release.json').read_text())
    if not isinstance(manifest, dict):
        raise ValueError('Release manifest must be an object')
    required = dict(schema=1, source_commit=COMMIT, binary_name=NAME,
                    platform='linux-x86_64', patch_sha256=expected_patch,
                    binary_sha256=expected_sha)
    if any(manifest.get(key) != value for key, value in required.items()):
        raise ValueError('Release provenance does not match the pinned custom build')
    binary = directory / NAME
    if digest(binary) != expected_sha:
        raise ValueError('Binary checksum does not match the trusted release digest')
    with binary.open('rb') as stream:
        header = stream.read(20)
    if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01'
            or struct.unpack('<H', header[18:20])[0] != 62):
        raise ValueError('A Linux x86_64 ELF binary is required')
    if not os.access(binary, os.X_OK):
        raise ValueError('Candidate binary is not executable')
    return binary


def validate_profile(profile):
    if profile.is_symlink() or profile.resolve() != profile or not profile.is_dir():
        raise ValueError('Profile must be a real canonical directory')
    for root, dirs, files in os.walk(profile):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink():
                raise ValueError('Profile contains symbolic links')
            if path.is_file():
                regular(path)
            elif not path.is_dir():
                raise ValueError('Profile contains a non-regular entry')
    marker = profile / '.ayugram-inline-test'
    regular(marker)
    if marker.read_bytes() != MARKER:
        raise ValueError('Profile isolation marker is invalid')


def seed_profile(profile, preferences):
    """Fresh profile only; reinstall never changes an existing profile."""
    if profile.exists() or profile.is_symlink():
        validate_profile(profile)
        return
    profile.mkdir(mode=0o700)
    (profile / '.ayugram-inline-test').write_bytes(MARKER)
    (profile / 'tdata').mkdir(mode=0o700)
    (profile / 'tdata/ayu_settings.json').write_text(json.dumps(preferences) + '\n')
    (profile / '.remote-preferences-pending').write_text('1\n')


def launch_preferences(profile, preferences):
    """Repair the first unauthenticated launch once native core settings exist."""
    validate_profile(profile)
    settings = profile / 'tdata/ayu_settings.json'
    regular(settings)
    current = json.loads(settings.read_text())
    if not isinstance(current, dict):
        raise ValueError('Invalid existing preferences; preserved')
    pending = profile / '.remote-preferences-pending'
    native = any((profile / 'tdata' / name).is_file()
                 for name in ('settingss', 'settings0', 'settings1'))
    changed = False
    if pending.exists() and native:
        # First run has no login: the operator must Quit normally before logging in.
        current.update(preferences)
        changed = True
    ghosts = current.get('ghostModeSettings')
    if current.get('useGlobalGhostMode') is True and isinstance(ghosts, dict) and '0' not in ghosts:
        ghosts['0'] = preferences['ghostModeSettings']['0']
        changed = True
    if changed:
        temporary = settings.with_suffix('.tmp')
        if temporary.exists() or temporary.is_symlink():
            raise ValueError('Unexpected preferences staging file; preserved')
        with temporary.open('x') as stream:
            json.dump(current, stream)
        os.replace(temporary, settings)
    if pending.exists() and native:
        pending.unlink()


def backup_configuration_safe():
    for name in ('backup.env', 'restic-password'):
        path = Path('/etc/ayugram-remote') / name
        try:
            regular(path)
            info = path.stat()
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
                return False
        except (OSError, ValueError):
            return False
    return True


def installed_binary():
    regular(BASE / 'trusted.json')
    trusted = json.loads((BASE / 'trusted.json').read_text())
    if (not isinstance(trusted, dict)
            or any(not isinstance(trusted.get(key), str)
                   or not re.fullmatch('[0-9a-f]{64}', trusted[key])
                   for key in ('binary_sha256', 'patch_sha256'))):
        raise ValueError('Invalid installed provenance hashes')
    return verify_release(BASE / 'releases' / trusted['binary_sha256'],
                          trusted['binary_sha256'], trusted['patch_sha256'])


def systemctl(*args):
    return subprocess.run(['/usr/bin/systemctl', *args], capture_output=True,
                          text=True, timeout=15, check=False)


def _history_module():
    path = BASE / 'history_archive.py'
    regular(path)
    info = path.stat()
    if (BASE.resolve() != BASE or BASE.stat().st_uid != 0 or BASE.stat().st_mode & 0o022
            or info.st_uid != 0 or info.st_mode & 0o022):
        raise ValueError('History helper must be installed as immutable root-owned code')
    spec = importlib.util.spec_from_file_location('history_archive', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def history_control(action, stream):
    """Read only this VPS archive; never accept a client-supplied file path."""
    if action not in ('history-catalog', 'history-export'):
        raise ValueError('Unsupported history action')
    data = stream.read(1025)
    if len(data) > 1024:
        raise ValueError('History request exceeds its size limit')
    request = json.loads(data)
    fields = set() if action == 'history-catalog' else {'user_id', 'known_digest'}
    if not isinstance(request, dict) or set(request) != fields:
        raise ValueError('Invalid history request fields')
    module = _history_module()
    # SQLite may need its usual WAL shared-memory file. Use the client UID,
    # never root, so a read snapshot cannot leave root-owned sidecars behind.
    if os.geteuid() == 0:
        account = pwd.getpwnam('ayugram-remote')
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    marker = PROFILE / '.ayugram-inline-test'
    regular(marker)
    if PROFILE.resolve() != PROFILE or marker.read_bytes() != MARKER:
        raise ValueError('Invalid isolated archive profile')
    archive = PROFILE / 'tdata/ayudata.db'
    if action == 'history-catalog':
        result = (module.catalog(archive) if os.path.lexists(archive) else
                  dict(protocol=1, schema_version=1, accounts=[]))
    else:
        result = module.export_archive(archive, request['user_id'], request['known_digest'])
    return dict(schema=1, ok=True, archive=result)


def control(action):
    if action == 'backup':
        if not backup_configuration_safe():
            raise ValueError('Backup configuration is missing or unsafe')
        installed_binary()
        validate_profile(PROFILE)
        result = systemctl('start', '--no-block', 'ayugram-remote-backup.service')
        if result.returncode:
            raise ValueError('Could not queue the backup service')
        return dict(schema=1, ok=True, queued=True)
    if action != 'status':
        raise ValueError('Only status and backup are permitted')
    verified = False
    try:
        installed_binary()
        validate_profile(PROFILE)
        verified = True
    except (OSError, ValueError, KeyError):
        pass
    def query(*args):
        try:
            return systemctl(*args)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def unit_state(unit):
        reply = query('is-active', unit)
        value = reply.stdout.strip() if reply is not None else ''
        # is-active uses exit status 3 for known non-active states.
        return value if (reply is not None and reply.returncode in (0, 3)
                         and value in UNIT_STATES) else 'unknown'

    states = [unit_state(unit) for unit in UNITS]
    active = [value == 'active' for value in states]
    backup = query('show', 'ayugram-remote-backup.service',
                   '--property=ActiveState,Result,ExecMainExitTimestamp')
    fields = (dict(line.split('=', 1) for line in backup.stdout.splitlines() if '=' in line)
              if backup is not None and backup.returncode == 0 else {})
    timer_active = unit_state('ayugram-remote-backup.timer') == 'active'
    configured = backup_configuration_safe() and timer_active
    finished = fields.get('ExecMainExitTimestamp', '')
    result = 'unknown'
    if (fields.get('ActiveState') in UNIT_STATES and fields.get('Result')
            and 'ExecMainExitTimestamp' in fields):
        if fields['Result'] != 'success':
            result = fields['Result']
        elif finished and fields['ActiveState'] != 'failed':
            result = 'success'
        elif not finished and fields['ActiveState'] == 'inactive':
            result = 'never'
    disk_total = disk_free = None
    try:
        disk = os.statvfs(PROFILE)
        disk_total = disk.f_blocks * disk.f_frsize
        disk_free = disk.f_bavail * disk.f_frsize
    except OSError:
        pass
    viewer_ready = verified and all(active) and configured
    needs_initialization = (PROFILE / '.remote-preferences-pending').exists()
    return dict(schema=1, ok=True, ready=viewer_ready and not needs_initialization,
                viewer_ready=viewer_ready, needs_initialization=needs_initialization,
                backup_configured=configured,
                client_active=active[0], client_state=states[0], vnc_port=5905,
                disk_total_bytes=disk_total, disk_free_bytes=disk_free,
                backup_active=fields.get('ActiveState') in ('activating', 'active'),
                backup_last_result=result, backup_last_finished=finished)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('One fixed action is required')
        if sys.argv[1] == 'launch':
            # CLOEXEC releases the startup lock only once exec installs the client.
            with open('/var/lib/ayugram-remote/launch.lock', 'rb') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                binary = installed_binary()
                launch_preferences(PROFILE, json.loads((BASE / 'preferences.json').read_text()))
                os.execv(str(binary), [str(binary), '-noupdate', '-ghost', '-workdir', str(PROFILE)])
        elif sys.argv[1] in ('verify', 'verify-backup'):
            installed_binary()
            validate_profile(PROFILE)
            if sys.argv[1] == 'verify-backup' and not backup_configuration_safe():
                raise ValueError('Unsafe backup configuration')
        elif sys.argv[1] in ('history-catalog', 'history-export'):
            print(json.dumps(history_control(sys.argv[1], sys.stdin.buffer), separators=(',', ':')))
        else:
            print(json.dumps(control(sys.argv[1])))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired):
        # Do not include paths, credential values or subprocess output in helper replies.
        print(json.dumps(dict(schema=1, ok=False, error='Remote operation failed; inspect the service locally')))
        sys.exit(1)
