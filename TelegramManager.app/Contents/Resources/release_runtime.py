"""Offline, app-only release transactions. Also copied outside the running bundle."""
import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

COMPONENTS = {
    'manager': ('TelegramManager.app', 'com.local.telegram-manager'),
    'ayugram-custom': ('AyuGram.app', 'one.telegrammanager.AyuGramInlineTest'),
}
EXECUTABLES = {'manager': 'launcher.sh', 'ayugram-custom': 'AyuGram Inline Test'}
MAX_EXPANDED = 5 * 1024**3
MAX_FILES = 30000
FORBIDDEN = {'tdata', 'telegramforceportable', 'backups', 'manager_config.json',
             'manager_data.json', 'manager_workspaces.json', '.git'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Invalid release version')
    return tuple(map(int, value.split('.')))


def state_dir(manager):
    manager = Path(manager).absolute()
    if manager.is_symlink() or manager.suffix != '.app':
        raise ValueError('Unsafe Manager bundle')
    parent = manager.parent.resolve()
    data = parent / 'data' if (parent / 'data').exists() else parent
    if data.is_symlink():
        raise ValueError('Unsafe Manager data directory')
    return data / 'ReleaseUpdates'


def safe(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Symbolic update path refused')
    if path.exists():
        info = path.stat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or (path.is_file() and info.st_nlink != 1):
            raise ValueError('Unsafe update file')
    return path


def workspace(manager):
    root = safe(state_dir(manager))
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    path = safe(path)
    fd, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        safe(path)
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(path):
    path = safe(path)
    if not path.exists():
        return {}
    if path.stat().st_size > 256 * 1024:
        raise ValueError('Update metadata is too large')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('Invalid update metadata')
    return value


def lease(manager, exclusive=False, blocking=True, name='lifecycle.lock'):
    path = safe(workspace(manager) / name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise ValueError('Unsafe update lock')
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))
        return os.fdopen(fd, 'r+')
    except BaseException:
        os.close(fd)
        raise


def fingerprint(app):
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(app):
        dirs.sort()
        for name in sorted(dirs + files):
            path = os.path.join(root, name)
            digest.update(os.path.relpath(path, app).encode() + b'\0')
            if os.path.islink(path):
                digest.update(b'link:' + os.readlink(path).encode())
            elif os.path.isfile(path):
                with open(path, 'rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        digest.update(block)
    return digest.hexdigest()


def validate_tree(app):
    app = safe(app)
    if not app.is_dir():
        raise ValueError('Application bundle is missing')
    base = app.resolve()
    count = total = 0
    for parent, dirs, files in os.walk(app, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            count += 1
            if name.lower() in FORBIDDEN:
                raise ValueError('Account data or runtime files in application package')
            info = path.lstat()
            if path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except (RuntimeError, OSError) as error:
                    raise ValueError('Broken or cyclic application symlink') from error
                if os.path.isabs(os.readlink(path)) or not resolved.is_relative_to(base):
                    raise ValueError('Application symlink escapes its bundle')
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError('Hardlinked application file refused')
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError('Special file in application package')
            if count > MAX_FILES or total > MAX_EXPANDED:
                raise ValueError('Application exceeds size limit')


def validate_manifest(value, component=None):
    component = component or value.get('component')
    if value.get('schema') != 1 or component not in COMPONENTS or value.get('component') != component:
        raise ValueError('Unsupported release manifest')
    version(value.get('version'))
    if value.get('bundle') != COMPONENTS[component][0] or value.get('bundle_id') != COMPONENTS[component][1]:
        raise ValueError('Release component identity mismatch')
    if value.get('archive') != component + '.tar.gz':
        raise ValueError('Unexpected release archive')
    for key, length in [('fingerprint', 64), ('source_commit', 40), ('sha256', 64)]:
        if not re.fullmatch('[0-9a-f]{' + str(length) + '}', str(value.get(key, ''))):
            raise ValueError('Missing release provenance: ' + key)
    if type(value.get('size')) is not int or not 0 < value['size'] <= 2 * 1024**3:
        raise ValueError('Invalid release archive size')
    if value.get('architecture') not in ('arm64', 'x86_64') or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', str(value.get('min_macos', ''))):
        raise ValueError('Unsupported platform metadata')
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', str(value.get('app_version', ''))):
        raise ValueError('Invalid application version')
    if component == 'manager':
        if value.get('data_compatibility') != 'manager-state-v1' or value['app_version'] != value['version']:
            raise ValueError('Unsupported Manager data contract')
    else:
        for key, length in [('patch_sha256', 64), ('upstream_commit', 40)]:
            if not re.fullmatch('[0-9a-f]{' + str(length) + '}', str(value.get(key, ''))):
                raise ValueError('Missing custom build provenance')
        if not re.fullmatch(r'ayugram-[a-zA-Z0-9.-]+', str(value.get('data_compatibility', ''))):
            raise ValueError('Missing custom data contract')
    return value


def verify_bundle(app, manifest, local_baseline=False):
    validate_tree(app)
    with open(Path(app) / 'Contents/Info.plist', 'rb') as source:
        info = plistlib.load(source)
    if info.get('CFBundleIdentifier') != manifest['bundle_id'] or info.get('CFBundleShortVersionString') != manifest['app_version']:
        raise ValueError('Application does not match the selected release')
    expected = EXECUTABLES[manifest['component']]
    if info.get('CFBundleExecutable') != expected:
        raise ValueError('Unexpected application executable')
    native = Path(app) / 'Contents/MacOS/launcher_swift'
    required = [expected] + (['launcher_swift'] if manifest['component'] == 'manager'
                             and (not local_baseline or native.exists() or native.is_symlink()) else [])
    for name in required:
        executable = safe(Path(app) / 'Contents/MacOS' / name)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError('Application executable is missing or not executable')
    if fingerprint(app) != manifest['fingerprint']:
        raise ValueError('Application fingerprint mismatch')
    # An existing developer Manager may be unsigned; rollback is bound to its saved bytes.
    if local_baseline and manifest['component'] == 'manager':
        return
    result = subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError('Application signature verification failed')


def extract(package, destination, manifest):
    """Validate all names before writing; links are created after regular files."""
    destination = safe(destination)
    if destination.exists():
        raise ValueError('Extraction directory already exists')
    with tarfile.open(package, 'r:gz') as archive:
        members, names, links = [], set(), set()
        total = 0
        for member in archive:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] != manifest['bundle'] or '\\' in member.name
                    or any(p.lower() in FORBIDDEN for p in path.parts)):
                raise ValueError('Unsafe archive path')
            name = str(path)
            if name in names or not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError('Duplicate or unsupported archive entry')
            if member.mode & 0o7000:
                raise ValueError('Privileged archive mode refused')
            if member.issym():
                target = PurePosixPath(member.linkname)
                normalized = os.path.normpath(str(path.parent / target))
                if target.is_absolute() or not normalized.startswith(manifest['bundle'] + '/') or '\\' in member.linkname:
                    raise ValueError('Archive link escapes bundle')
                links.add(name)
            total += member.size
            names.add(name)
            members.append(member)
            if len(members) > MAX_FILES or total > MAX_EXPANDED or member.size < 0:
                raise ValueError('Expanded archive exceeds limit')
        if any(str(parent) in links for name in names for parent in PurePosixPath(name).parents):
            raise ValueError('Archive writes through a symlink')
        destination.mkdir(mode=0o700)
        try:
            for member in members:
                path = destination / member.name
                if member.issym():
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    path.mkdir(exist_ok=True)
                else:
                    with archive.extractfile(member) as source, path.open('xb') as target:
                        shutil.copyfileobj(source, target)
                os.chmod(path, member.mode & 0o777)
                os.utime(path, (member.mtime, member.mtime))
            for member in members:
                if member.issym():
                    path = destination / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.symlink_to(member.linkname)
            app = destination / manifest['bundle']
            verify_bundle(app, manifest)
            return app
        except BaseException:
            shutil.rmtree(destination)
            raise


def exchange(first, second):
    """Darwin atomic swap keeps the Manager bundle present across a crash."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(first), os.fsencode(second), 2):  # RENAME_SWAP
        raise OSError(ctypes.get_errno(), 'Atomic application exchange failed')
    sync_dir(Path(first).parent)
    sync_dir(Path(second).parent)


def target_path(manager, component):
    if component not in COMPONENTS:
        raise ValueError('Unknown release component')
    if component == 'manager':
        return safe(manager)
    base = state_dir(manager).parent
    for part in ('_apps', 'macOS'):
        base = safe(base / part)
        base.mkdir(exist_ok=True)
    return safe(base / 'AyuGram.app')


def metadata_path(manager, component):
    return (workspace(manager) / 'manager-installed.json' if component == 'manager'
            else target_path(manager, component).parent / '.ayugram-official.json')


def native_metadata(manifest):
    if manifest['component'] == 'manager':
        return manifest
    return {'release': manifest['app_version'], 'fingerprint': manifest['fingerprint'],
            'kind': 'local-inline', 'bundle_id': manifest['bundle_id'],
            'source_commit': manifest['upstream_commit'], 'patch_sha256': manifest['patch_sha256'],
            'custom_release': manifest}


def clients_closed(manager):
    result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise ValueError('Running applications could not be verified')
    manager = str(Path(manager).resolve())
    for row in result.stdout.splitlines():
        fields = row.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            raise ValueError('Unexpected process listing')
        if int(fields[0]) == os.getpid():
            continue
        command = fields[1]
        if (manager + '/Contents/MacOS/' in command or manager + '/Contents/Resources/server.py' in command
                or re.search(r'/Contents/MacOS/(?:Telegram|AyuGram)(?:\s|$)', command)):
            return False
    return True


def paths(manager):
    root = workspace(manager)
    return root, safe(root / 'job.json'), safe(root / 'transaction.json')


def restore_metadata(path, value):
    if value:
        save(path, value)
    elif safe(path).exists():
        path.unlink()
        sync_dir(path.parent)


def require_no_legacy_update(manager, component):
    if component == 'ayugram-custom' and safe(target_path(manager, component).parent / '.ayugram-update.json').exists():
        raise ValueError('An interrupted official AyuGram update must be recovered in Manager first')


def rollback_snapshot(app, component, metadata):
    if not app.exists():
        return {}
    validate_tree(app)
    with open(app / 'Contents/Info.plist', 'rb') as source:
        info = plistlib.load(source)
    marker = read(app / 'Contents/Resources/managed-release.json') if component == 'manager' else metadata
    return {'schema': 1, 'kind': 'local-rollback', 'component': component,
            'bundle': COMPONENTS[component][0], 'bundle_id': info.get('CFBundleIdentifier'),
            'app_version': info.get('CFBundleShortVersionString'), 'fingerprint': fingerprint(app),
            'data_compatibility': marker.get('data_compatibility', metadata.get('data_compatibility'))}


def validate_snapshot(value, component):
    if (not isinstance(value, dict) or value.get('component') != component
            or value.get('bundle') != COMPONENTS[component][0]
            or value.get('bundle_id') != COMPONENTS[component][1]
            or not re.fullmatch('[0-9a-f]{64}', str(value.get('fingerprint', '')))
            or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', str(value.get('app_version', '')))):
        raise ValueError('Invalid local rollback snapshot')
    return value


def recover(manager):
    root, jobpath, journal = paths(manager)
    record = read(journal)
    if not record:
        return False
    if record.get('committed'):
        finish_commit(manager, record)
        return True
    component = record['component']
    target = target_path(manager, component)
    candidate = safe(root / 'candidate' / COMPONENTS[component][0])
    # The journal is written before swapping; determine which side holds old bytes.
    current = fingerprint(target) if target.exists() else None
    if current == record['new_fingerprint']:
        if record['old_fingerprint'] is None:
            target.rename(candidate)
        elif candidate.exists() and fingerprint(candidate) == record['old_fingerprint']:
            exchange(target, candidate)
        else:
            raise ValueError('Cannot locate the verified previous application')
    elif current != record['old_fingerprint']:
        raise ValueError('Application changed outside the interrupted update')
    restore_metadata(metadata_path(manager, component), record['old_metadata'])
    journal.unlink()
    save(jobpath, {'status': 'failed', 'component': component,
                   'message': 'Interrupted installation rolled back. Schedule the release again.'})
    return True


def rollback_available(manager, component):
    record = read(workspace(manager) / (component + '-previous.json'))
    if not record or record.get('used'):
        return False
    try:
        validate_snapshot(record.get('manifest', {}), component)
    except (ValueError, TypeError):
        return False
    if component == 'manager':
        current = read(metadata_path(manager, component)) or read(Path(manager) / 'Contents/Resources/managed-release.json')
        return (current.get('data_compatibility') == 'manager-state-v1'
                and record.get('manifest', {}).get('data_compatibility') == 'manager-state-v1')
    return True


def mark_ayugram_launch(manager):
    if not (state_dir(manager) / 'ayugram-custom-previous.json').exists():
        return
    with lease(manager, exclusive=True, name='state.lock'):
        path = workspace(manager) / 'ayugram-custom-previous.json'
        record = read(path)
        if record and not record.get('used'):
            record['used'] = True
            save(path, record)


def queue_rollback(manager, component):
    if component not in COMPONENTS:
        raise ValueError('Unknown release component')
    with lease(manager, exclusive=True, name='state.lock'):
        root, jobpath, journal = paths(manager)
        if read(journal) or read(jobpath).get('status') == 'queued':
            raise ValueError('Finish or cancel the pending update first')
        if not rollback_available(manager, component):
            raise ValueError('No verified compatible rollback is available')
        record = read(root / (component + '-previous.json'))
        require_no_legacy_update(manager, component)
        manifest = validate_snapshot(record['manifest'], component)
        previous = safe(root / (component + '-previous.app'))
        verify_bundle(previous, manifest, local_baseline=True)
        candidate = safe(root / 'candidate')
        if candidate.exists():
            shutil.rmtree(candidate)
        candidate.mkdir(mode=0o700)
        shutil.copytree(previous, candidate / manifest['bundle'], symlinks=True)
        target = target_path(manager, component)
        job = {'status': 'queued', 'component': component, 'manifest': manifest,
               'destination_metadata': record['metadata'],
               'rollback': True, 'baseline': fingerprint(target),
               'message': 'Rollback scheduled. Quit Manager and all Telegram/AyuGram clients normally.'}
        save(jobpath, job)
        return job


def apply_job(manager):
    root, jobpath, journal = paths(manager)
    with lease(manager, exclusive=True, blocking=False), lease(manager, exclusive=True, name='state.lock'):
        if not clients_closed(manager):
            return False
        if recover(manager):
            return True
        job = read(jobpath)
        if job.get('status') != 'queued':
            return True
        component = job['component']
        require_no_legacy_update(manager, component)
        target = target_path(manager, component)
        candidate = safe(root / 'candidate' / COMPONENTS[component][0])
        manifest = (validate_snapshot(job['manifest'], component) if job.get('rollback')
                    else validate_manifest(job['manifest'], component))
        if (fingerprint(target) if target.exists() else None) != job['baseline']:
            raise ValueError('Installed app changed after scheduling; check releases again')
        if job.get('rollback') and not rollback_available(manager, component):
            raise ValueError('Rollback is no longer safe after application use')
        if job.get('rollback'):
            previous = read(root / (component + '-previous.json'))
            if manifest != previous['manifest'] or job.get('destination_metadata') != previous['metadata']:
                raise ValueError('Rollback metadata changed after scheduling')
        verify_bundle(candidate, manifest, local_baseline=bool(job.get('rollback')))
        old_metadata = read(metadata_path(manager, component))
        old_manifest = rollback_snapshot(target, component, old_metadata)
        old = fingerprint(target) if target.exists() else None
        record = {'component': component, 'old_fingerprint': old,
                  'new_fingerprint': manifest['fingerprint'], 'old_metadata': old_metadata,
                  'old_manifest': old_manifest}
        previous = safe(root / (component + '-previous.app'))
        # Preserve a pre-existing recovery slot until the new installation is committed.
        save(journal, record)
        try:
            if old is None:
                candidate.rename(target)
            else:
                validate_tree(target)
                exchange(target, candidate)
            restore_metadata(metadata_path(manager, component),
                             job['destination_metadata'] if job.get('rollback') else native_metadata(manifest))
            # Mark committed before moving the old app; recovery never needs a missing target.
            record['committed'] = True
            save(journal, record)
            finish_commit(manager, record, old_manifest)
            return True
        except BaseException:
            if not read(journal).get('committed'):
                recover(manager)
            raise


def finish_commit(manager, record, old_manifest=None):
    root, jobpath, journal = paths(manager)
    component = record['component']
    candidate = safe(root / 'candidate' / COMPONENTS[component][0])
    previous = safe(root / (component + '-previous.app'))
    old_manifest = old_manifest if old_manifest is not None else record['old_manifest']
    if candidate.exists():
        if fingerprint(candidate) != record['old_fingerprint']:
            raise ValueError('Previous application changed during commit')
        if previous.exists():
            validate_tree(previous)
            shutil.rmtree(previous)
        candidate.rename(previous)
    save(root / (component + '-previous.json'), {'manifest': old_manifest,
         'metadata': record['old_metadata'], 'fingerprint': record['old_fingerprint'], 'used': False})
    save(jobpath, {'status': 'installed', 'component': component,
                   'message': 'Application installed. Previous app retained; sessions were not modified.'})
    journal.unlink()
    sync_dir(root)


def worker(manager):
    try:
        worker_lock = lease(manager, exclusive=True, blocking=False, name='worker.lock')
    except BlockingIOError:
        return
    with worker_lock:
        while True:
            try:
                # Finish a committed transaction without reverting the installed release.
                with lease(manager, exclusive=True, blocking=False), lease(manager, exclusive=True, name='state.lock'):
                    if clients_closed(manager):
                        record = read(paths(manager)[2])
                        if record.get('committed'):
                            finish_commit(manager, record)
                            return
                if apply_job(manager):
                    return
            except BlockingIOError:
                pass
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
                with lease(manager, exclusive=True, name='state.lock'):
                    job = read(paths(manager)[1])
                    if job.get('status') == 'queued':
                        save(paths(manager)[1], {**job, 'status': 'failed', 'message': str(error)})
                return
            time.sleep(5)


def launch(manager, command):
    # Hold the lease in a supervising process; Swift also protects direct native launches.
    if read(paths(manager)[2]):
        worker(manager)
    with lease(manager):
        if read(paths(manager)[2]):
            raise ValueError('Update recovery is incomplete; close clients and retry')
        return subprocess.call(command)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['worker', 'launch', 'rollback'])
    parser.add_argument('manager')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'rollback':
        if len(args.command) != 1:
            parser.error('rollback requires one component: manager or ayugram-custom')
        queue_rollback(args.manager, args.command[0])
        worker(args.manager)
    elif args.action == 'worker':
        worker(args.manager)
    else:
        os.environ['TG_RELEASE_LAUNCH_GUARD'] = '1'
        sys.exit(launch(args.manager, args.command))
