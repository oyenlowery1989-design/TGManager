#!/usr/bin/env python3
"""Install reviewed files locally on Debian 12; never connects to any server."""
import argparse
import ast
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys

from runtime import NAME, digest, seed_profile, verify_release

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def common_preferences():
    # Read the literal without importing Manager or touching its account/state files.
    source = ROOT / 'TelegramManager.app/Contents/Resources/ayugram.py'
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'COMMON_PROFILE' for target in node.targets):
            result = ast.literal_eval(node.value)
            result['semiTransparentDeletedMessages'] = True
            return result
    raise ValueError('Manager common preferences were not found')


def run(*args):
    subprocess.run(args, check=True)


def safe_file(path, uid=0, modes=None):
    """Existing destinations must be singly linked, correctly owned files."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
            or info.st_mode & 0o022
            or (modes is not None and stat.S_IMODE(info.st_mode) not in modes)):
        raise ValueError('Unsafe existing installation file: ' + str(path))


def safe_directory(path):
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Installation directory has a linked ancestor: ' + str(path))
    if path.is_symlink() or (path.exists() and (not path.is_dir()
            or path.stat().st_uid != 0 or path.stat().st_mode & 0o022)):
        raise ValueError('Unsafe installation directory: ' + str(path))


def root_copy(source, destination, mode):
    safe_file(destination)
    shutil.copyfile(source, destination)
    os.chown(destination, 0, 0)
    destination.chmod(mode)


def root_write(destination, contents, mode=0o644):
    safe_file(destination)
    destination.write_text(contents)
    os.chown(destination, 0, 0)
    destination.chmod(mode)


def require_stopped():
    units = [source.name for source in (HERE / 'systemd').glob('*.service')]
    units += ['ayugram-remote.target', 'ayugram-remote-backup.timer']
    for unit in units:
        # Include transitional states and the upholding target, not just live PIDs.
        result = subprocess.run(['/usr/bin/systemctl', 'show', unit,
                                 '--property=ActiveState', '--value'],
                                capture_output=True, text=True, timeout=15, check=False)
        if result.returncode or result.stdout.strip() not in ('inactive', 'failed'):
            raise ValueError('Stop the AyuGram target, backup timer and backup service before installation')


def install(candidate, expected_sha):
    if os.geteuid() != 0:
        raise ValueError('Run this reviewed installer as root on Debian 12')
    if os.uname().machine != 'x86_64' or 'VERSION_ID="12"' not in Path('/etc/os-release').read_text():
        raise ValueError('Debian 12 x86_64 is required')
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
        raise ValueError('A unified cgroup v2 hierarchy is required for bounded backups')
    patch_sha = digest(ROOT / 'experimental/ayugram-inline/ayugram-inline.patch')
    binary = verify_release(candidate.resolve(), expected_sha, patch_sha)
    preferences = common_preferences()
    for program in ('Xvfb', 'openbox', 'x11vnc', 'xauth', 'xdpyinfo', 'restic', 'rsync', 'systemctl', 'dbus-run-session'):
        if not shutil.which(program):
            raise ValueError('Missing prerequisite: ' + program)
    require_stopped()
    base = Path('/opt/ayugram-remote')
    home = Path('/var/lib/ayugram-remote')
    config = Path('/etc/ayugram-remote')
    release = base / 'releases' / expected_sha
    staging = home / 'backup-staging'
    directories = (base, base / 'releases', home, config, release, staging,
                   Path('/etc/systemd/system'), Path('/usr/local/sbin'))
    # Check every existing destination before creating a user, writing or enabling.
    for directory in directories:
        safe_directory(directory)
    units = list((HERE / 'systemd').iterdir())
    code_files = [base / name for name in ('runtime.py', 'history_archive.py', 'backup.sh', 'wait-display.sh',
                                          'preferences.json', 'trusted.json')]
    resolver = config / 'runtime-resolv.conf'
    code_files += [resolver, release / NAME, release / 'release.json',
                   Path('/usr/local/sbin/ayugram-remote-control')]
    code_files += [Path('/etc/systemd/system') / source.name for source in units]
    for path in code_files:
        safe_file(path)
    temporary = base / 'trusted.json.tmp'
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('Unexpected provenance staging file')
    for name in ('backup.env', 'restic-password', 'gcs-credentials.json'):
        safe_file(config / name, modes={0o600})
    try:
        account = pwd.getpwnam('ayugram-remote')
    except KeyError:
        account = None
    if account and (account.pw_uid == 0 or account.pw_dir != str(home)
                    or account.pw_shell != '/usr/sbin/nologin'):
        raise ValueError('Existing runtime user has unexpected properties')
    lock = home / 'launch.lock'
    safe_file(lock, modes={0o640})
    authority = home / 'Xauthority'
    if authority.exists() or authority.is_symlink():
        if account is None:
            raise ValueError('Existing Xauthority has no matching runtime user')
        safe_file(authority, uid=account.pw_uid, modes={0o600})
        if authority.stat().st_gid != account.pw_gid:
            raise ValueError('Existing Xauthority has an unexpected group')
    vnc = config / 'vnc-password'
    safe_file(vnc, modes={0o600, 0o640})
    if not vnc.is_file() or vnc.stat().st_size != 8:
        raise ValueError('Create the private VNC password file interactively first; see deployment guide')
    if stat.S_IMODE(vnc.stat().st_mode) == 0o640 and (account is None or vnc.stat().st_gid != account.pw_gid):
        raise ValueError('Existing VNC password has an unexpected group')
    if release.exists():
        verify_release(release, expected_sha, patch_sha)
    for directory in directories[:4]:
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        directory.chmod(0o755)
    if account is None:
        run('/usr/sbin/useradd', '--system', '--home-dir', str(home),
            '--shell', '/usr/sbin/nologin', '--user-group', 'ayugram-remote')
        account = pwd.getpwnam('ayugram-remote')
    profile = home / 'profile'
    fresh = not profile.exists()
    seed_profile(profile, preferences)
    if fresh:
        for root, dirs, files in os.walk(profile):
            os.chown(root, account.pw_uid, account.pw_gid)
            for name in files:
                path = Path(root) / name
                os.chown(path, account.pw_uid, account.pw_gid)
                path.chmod(0o600)
    if not release.exists():
        release.mkdir(mode=0o755)
        release.chmod(0o755)
        root_copy(binary, release / NAME, 0o755)
        root_copy(candidate / 'release.json', release / 'release.json', 0o644)
        verify_release(release, expected_sha, patch_sha)
    for name in ('runtime.py', 'backup.sh', 'wait-display.sh'):
        root_copy(HERE / name, base / name, 0o755)
    root_copy(ROOT / 'TelegramManager.app/Contents/Resources/history_archive.py',
              base / 'history_archive.py', 0o644)
    root_write(resolver, 'nameserver 8.8.8.8\nnameserver 8.8.4.4\noptions timeout:2 attempts:2\n')
    root_write(base / 'preferences.json', json.dumps(preferences) + '\n')
    root_write(temporary, json.dumps(dict(binary_sha256=expected_sha, patch_sha256=patch_sha)) + '\n')
    os.replace(temporary, base / 'trusted.json')
    # Root owns the parent so the runtime user cannot replace backup-staging.
    os.chown(home, 0, account.pw_gid)
    home.chmod(0o750)
    staging.mkdir(mode=0o700, exist_ok=True)
    os.chown(staging, 0, 0)
    staging.chmod(0o700)
    if not lock.exists():
        lock.touch(mode=0o640)
    os.chown(lock, 0, account.pw_gid)
    lock.chmod(0o640)
    if not authority.exists():
        import secrets
        subprocess.run(['/usr/bin/xauth', '-f', str(authority)],
                       input='add :95 . ' + secrets.token_hex(16) + '\n',
                       text=True, check=True, stdout=subprocess.DEVNULL)
        authority.chmod(0o600)
        os.chown(authority, account.pw_uid, account.pw_gid)
    os.chown(vnc, 0, account.pw_gid)
    vnc.chmod(0o640)
    for source in units:
        root_copy(source, Path('/etc/systemd/system') / source.name, 0o644)
    root_copy(HERE / 'ayugram-remote-control', Path('/usr/local/sbin/ayugram-remote-control'), 0o755)
    run('/usr/bin/systemctl', 'daemon-reload')
    # Enable only after provenance, profile, display authentication and units exist.
    run('/usr/bin/systemctl', 'enable', 'ayugram-remote.target', 'ayugram-remote-backup.timer')
    print('Installed and enabled for next boot. Configure backup, then start the target explicitly. Existing profiles were preserved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('candidate', type=Path, help='Directory containing the binary and release.json')
    parser.add_argument('--sha256', required=True, help='Trusted binary digest obtained from the reviewed build')
    args = parser.parse_args()
    try:
        if len(args.sha256) != 64 or any(c not in '0123456789abcdef' for c in args.sha256):
            raise ValueError('A lowercase SHA-256 digest is required')
        install(args.candidate, args.sha256)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Installation failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
