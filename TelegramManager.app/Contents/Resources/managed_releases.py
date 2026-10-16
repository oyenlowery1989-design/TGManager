"""Fixed-repository release checks and user-approved, deferred app updates."""
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

import release_runtime as runtime
import state

REPOSITORY = 'oyenlowery1989-design/TGManager'
LABELS = {'manager': 'Manager', 'ayugram-custom': 'AyuGram Custom'}
_CHECK_LOCK = threading.Lock()


def manager():
    return Path(state.APP_BUNDLE)


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if (parsed.scheme != 'https' or parsed.hostname not in
                ('github.com', 'api.github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')
                or parsed.username or parsed.password):
            raise ValueError('Untrusted release redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit, digest=None, destination=None):
    if not (url.startswith(f'https://api.github.com/repos/{REPOSITORY}/')
            or url.startswith(f'https://github.com/{REPOSITORY}/releases/download/')):
        raise ValueError('Untrusted release URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'TelegramManager', 'Accept': 'application/vnd.github+json'})
    chunks, count, checksum = [], 0, hashlib.sha256()
    deadline = time.monotonic() + 900
    output = open(destination, 'xb') if destination else None
    try:
        with urllib.request.build_opener(ReleaseRedirect()).open(request, timeout=30) as response:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > limit or time.monotonic() > deadline:
                    raise ValueError('Release download exceeded its size or time limit')
                checksum.update(block)
                if output:
                    output.write(block)
                else:
                    chunks.append(block)
        if digest and checksum.hexdigest() != digest:
            raise ValueError('Release failed SHA-256 verification')
        return count if output else b''.join(chunks)
    finally:
        if output:
            output.close()


def asset(release, name):
    matches = [a for a in release.get('assets', []) if isinstance(a, dict) and a.get('name') == name]
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous release asset: ' + name)
    item = matches[0]
    url = f'https://github.com/{REPOSITORY}/releases/download/{release["tag_name"]}/{name}'
    digest = item.get('digest', '')
    import re
    if item.get('browser_download_url') != url or not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Release asset has no trusted URL and checksum')
    if type(item.get('size')) is not int or item['size'] <= 0:
        raise ValueError('Invalid release asset size')
    return url, digest[7:], item['size']


def releases():
    data = json.loads(download(f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=100', 2 * 1024**2))
    if not isinstance(data, list):
        raise ValueError('Invalid repository release listing')
    selected = {}
    for release in data:
        if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
            continue
        for component in LABELS:
            prefix = component + '-v'
            tag = release.get('tag_name', '')
            if not isinstance(tag, str) or not tag.startswith(prefix):
                continue
            try:
                key = runtime.version(tag[len(prefix):])
            except ValueError:
                continue
            if component not in selected or key > selected[component][0]:
                selected[component] = key, release
    return {key: value[1] for key, value in selected.items()}


def manifest_for(component, release):
    url, digest, size = asset(release, component + '.json')
    if size > 64 * 1024:
        raise ValueError('Release manifest is too large')
    manifest = runtime.validate_manifest(json.loads(download(url, 64 * 1024, digest)), component)
    if release['tag_name'] != component + '-v' + manifest['version']:
        raise ValueError('Release tag and manifest disagree')
    url, digest, size = asset(release, manifest['archive'])
    if manifest['sha256'] != digest or manifest['size'] != size:
        raise ValueError('Manifest and published package disagree')
    return {**manifest, 'download_url': url, 'release_url': f'https://github.com/{REPOSITORY}/releases/tag/{release["tag_name"]}'}


def installed(component):
    if component == 'manager':
        value = runtime.read(manager() / 'Contents/Resources/managed-release.json')
    else:
        value = runtime.read(runtime.metadata_path(manager(), component)).get('custom_release', {})
    return value.get('version')


def check_releases():
    try:
        available = releases()
        failure = None
    except (OSError, ValueError, TypeError) as error:
        available, failure = {}, str(error)
    rows = []
    for component, label in LABELS.items():
        row = {'client': label, 'component': component, 'managed_release': True,
               'installed_version': installed(component), 'update_available': False}
        try:
            if failure:
                raise ValueError(failure)
            if component in available:
                manifest = manifest_for(component, available[component])
                row.update(version=manifest['version'], release_url=manifest['release_url'])
                row['update_available'] = not row['installed_version'] or runtime.version(manifest['version']) > runtime.version(row['installed_version'])
            else:
                row['note'] = 'No published release in this channel yet.'
        except (OSError, ValueError, TypeError, KeyError) as error:
            row['error'] = str(error)
        rows.append(row)
    return rows


def _status_locked():
    root = runtime.workspace(manager())
    job = runtime.read(root / 'job.json')
    monitor = runtime.read(root / 'monitor.json')
    rows = monitor.get('clients', [])
    for row in rows:
        if row.get('component') in LABELS:
            row['installed_version'] = installed(row['component'])
            row['update_available'] = bool(row.get('version') and (not row['installed_version']
                or runtime.version(row['version']) > runtime.version(row['installed_version'])))
    return {'success': True, 'job': {k: job[k] for k in ('status', 'component', 'message') if k in job},
            'rollback': {c: runtime.rollback_available(manager(), c) for c in LABELS},
            'last_checked': monitor.get('last_checked'), 'clients': rows,
            'error': monitor.get('error', '')}


def status():
    try:
        with runtime.lease(manager(), name='state.lock'):
            return _status_locked()
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'success': False, 'message': str(error)}


def spawn_worker():
    root = runtime.workspace(manager())
    helper = runtime.safe(root / 'worker.py')
    temporary = runtime.safe(root / 'worker.py.new')
    if temporary.exists():
        temporary.unlink()
    with temporary.open('xb') as out:
        out.write(Path(runtime.__file__).read_bytes())
    os.chmod(temporary, 0o700)
    os.replace(temporary, helper)
    with open(os.devnull, 'wb') as output:
        subprocess.Popen([sys.executable, str(helper), 'worker', str(manager())],
                         stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                         start_new_session=True, close_fds=True, cwd=root)


def ensure_worker():
    with runtime.lease(manager(), name='state.lock'):
        root = runtime.workspace(manager())
        if runtime.read(root / 'job.json').get('status') == 'queued' or runtime.read(root / 'transaction.json'):
            spawn_worker()


def run_scheduled_check(now=None, force=False):
    import updates
    now = time.time() if now is None else now
    if not _CHECK_LOCK.acquire(blocking=force):
        return
    try:
        root = runtime.workspace(manager())
        record = runtime.read(root / 'monitor.json')
        if not force and (record.get('last_checked', 0) >= updates._weekly_slot(now)[0] or now - record.get('last_attempt', 0) < 3600):
            return
        runtime.save(root / 'monitor.json', {**record, 'last_attempt': now})
        rows = check_releases()
        errors = [row['error'] for row in rows if row.get('error')]
        record.update(last_attempt=now, clients=rows, error='; '.join(errors))
        if not errors:
            record['last_checked'] = now
        runtime.save(root / 'monitor.json', record)
        return rows
    finally:
        _CHECK_LOCK.release()


def schedule(component, requested_version=None, rollback=False):
    import updates
    if component not in LABELS or type(rollback) is not bool:
        return {'success': False, 'message': 'Invalid release component'}
    if not updates.INSTALL_LOCK.acquire(blocking=False):
        return {'success': False, 'message': 'Another client operation is in progress'}
    staged = False
    try:
        root = runtime.workspace(manager())
        if rollback:
            job = runtime.queue_rollback(manager(), component)
            spawn_worker()
            return {'success': True, 'scheduled': True, 'message': job['message']}
        with runtime.lease(manager(), exclusive=True, name='state.lock'):
            if runtime.read(root / 'transaction.json'):
                raise ValueError('Finish interrupted update recovery before scheduling another release')
            if runtime.read(root / 'job.json').get('status') == 'queued':
                raise ValueError('An update is already scheduled. Cancel it first or let it finish.')
            target = runtime.target_path(manager(), component)
            release = releases().get(component)
            if not release:
                raise ValueError('No published release is available')
            manifest = manifest_for(component, release)
            if manifest['version'] != requested_version:
                raise ValueError('Release changed. Check again before scheduling.')
            current = installed(component)
            if current and runtime.version(manifest['version']) <= runtime.version(current):
                raise ValueError('This release is already installed or older')
            if manifest['architecture'] != platform.machine():
                raise ValueError('Release architecture does not match this Mac')
            os_version = platform.mac_ver()[0]
            if not os_version or tuple(map(int, os_version.split('.'))) < tuple(map(int, manifest['min_macos'].split('.'))):
                raise ValueError('Release requires a newer macOS version')
            if target.exists():
                with open(target / 'Contents/Info.plist', 'rb') as source:
                    current_app = plistlib.load(source).get('CFBundleShortVersionString')
                if not current_app or tuple(map(int, manifest['app_version'].split('.'))) < tuple(map(int, current_app.split('.'))):
                    raise ValueError('Application downgrade is not allowed')
            runtime.require_no_legacy_update(manager(), component)
            staged = True
            # Keep updater files separate from all account/session directories.
            candidate = runtime.safe(root / 'candidate')
            if candidate.exists():
                shutil.rmtree(candidate)
            package = runtime.safe(root / 'package.tar.gz')
            if package.exists():
                package.unlink()
            if shutil.disk_usage(root).free < manifest['size'] + runtime.MAX_EXPANDED + 512 * 1024**2:
                raise ValueError('Not enough free space to stage this release safely')
            count = download(manifest['download_url'], manifest['size'], manifest['sha256'], package)
            if count != manifest['size']:
                raise ValueError('Release archive size mismatch')
            runtime.extract(package, candidate, manifest)
            package.unlink()
            runtime.verify_bundle(candidate / manifest['bundle'], manifest)
            if target.exists():
                runtime.validate_tree(target)
            job = {'status': 'queued', 'component': component, 'manifest': manifest,
                   'rollback': rollback, 'baseline': runtime.fingerprint(target) if target.exists() else None,
                   'message': 'Scheduled. Quit Manager and all Telegram/AyuGram clients normally. Installation runs after they close.'}
            runtime.save(root / 'job.json', job)
            staged = False
            spawn_worker()
            return {'success': True, 'scheduled': True, 'message': job['message']}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        return {'success': False, 'message': str(error)}
    finally:
        try:
            if staged:
                with runtime.lease(manager(), exclusive=True, name='state.lock'):
                    if runtime.read(root / 'job.json').get('status') != 'queued':
                        candidate = runtime.safe(root / 'candidate')
                        if candidate.exists():
                            shutil.rmtree(candidate)
                        runtime.safe(root / 'package.tar.gz').unlink(missing_ok=True)
        except (OSError, ValueError) as error:
            state._log.warning('Release staging cleanup failed: %s', error)
        finally:
            updates.INSTALL_LOCK.release()


def cancel():
    with runtime.lease(manager(), exclusive=True, name='state.lock'):
        root = runtime.workspace(manager())
        if runtime.read(root / 'transaction.json'):
            return {'success': False, 'message': 'Installation has started; cancellation is unavailable'}
        runtime.save(root / 'job.json', {'status': 'cancelled', 'message': 'Scheduled update cancelled.'})
        candidate = runtime.safe(root / 'candidate')
        if candidate.exists():
            shutil.rmtree(candidate)
        runtime.safe(root / 'package.tar.gz').unlink(missing_ok=True)
        return {'success': True, 'message': 'Scheduled update cancelled.'}
