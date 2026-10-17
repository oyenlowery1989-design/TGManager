#!/usr/bin/python3
"""Fixed-path remote runtime: no session import or arbitrary command execution."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
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
    active = [systemctl('is-active', unit).stdout.strip() == 'active' for unit in UNITS]
    backup = systemctl('show', 'ayugram-remote-backup.service',
                       '--property=ActiveState,Result,ExecMainExitTimestamp')
    fields = dict(line.split('=', 1) for line in backup.stdout.splitlines() if '=' in line)
    timer_active = systemctl('is-active', 'ayugram-remote-backup.timer').stdout.strip() == 'active'
    configured = backup_configuration_safe() and timer_active
    finished = fields.get('ExecMainExitTimestamp', '')
    result = fields.get('Result', 'unknown') if finished else 'never'
    return dict(schema=1, ok=True, ready=verified and all(active) and configured,
                backup_configured=configured,
                client_active=active[0], vnc_port=5905,
                backup_active=fields.get('ActiveState') == 'activating',
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
        else:
            print(json.dumps(control(sys.argv[1])))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        # Do not include paths, credential values or subprocess output in helper replies.
        print(json.dumps(dict(schema=1, ok=False, error='Remote operation failed; inspect the service locally')))
        sys.exit(1)
