"""Official macOS client releases and controlled shared-app installation."""
import hashlib
import html
import stat
import functools
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlparse

import state

INSTALL_LOCK = threading.RLock()
REPOSITORIES = {'Telegram': 'telegramdesktop/tdesktop', 'AyuGram': 'AyuGram/AyuGramDesktop'}
MAX_DOWNLOAD = 1024 * 1024 * 1024


def guard_client_launch(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        if not INSTALL_LOCK.acquire(blocking=False):
            return False, 'Another client launch or update is in progress; try again shortly'
        try:
            if function.__module__ == 'ayugram':
                import release_runtime
                release_runtime.mark_ayugram_launch(state.APP_BUNDLE)
            return function(*args, **kwargs)
        finally:
            INSTALL_LOCK.release()
    return guarded


def _server():
    import server
    return server


def installed_version(client):
    server = _server()
    app = server.get_shared_app() if client == 'Telegram' else os.path.join(state.DATA_DIR, '_apps', 'macOS', 'AyuGram.app')
    return server._bundle_version(app) if app else None


def inline_history_installed():
    import ayugram
    return _server()._bundle_identifier(os.path.join(
        state.DATA_DIR, '_apps', 'macOS', 'AyuGram.app')) == ayugram.INLINE_BUNDLE_ID


def compatibility(telegram_version, ayugram_version):
    server = _server()
    telegram = server._version_tuple(telegram_version)
    ayu = server._version_tuple(ayugram_version)
    if not telegram or not ayu:
        return 'Session import compatibility is unknown because a client version is unavailable. Separate logins can still be used.'
    if telegram > ayu:
        return (f'Telegram {telegram_version} is newer than AyuGram {ayugram_version}. '
                'Sessions written by this Telegram may not import into AyuGram. '
                'Independently authenticated AyuGram profiles can still be used.')
    return ''


def _official_release_page(repo, tag=None):
    if tag is None:
        request = urllib.request.Request(f'https://github.com/{repo}/releases/latest', headers={'User-Agent': 'TelegramManager'})
        with urllib.request.urlopen(request, timeout=20) as response:
            final = response.geturl()
            raw = response.read(2 * 1024 * 1024 + 1)
        prefix = f'https://github.com/{repo}/releases/tag/'
        if len(raw) > 2 * 1024 * 1024 or not final.startswith(prefix) or not re.fullmatch(r'v?\d+\.\d+\.\d+', final[len(prefix):]):
            raise ValueError('Unexpected official release page redirect')
        tag = final[len(prefix):]
    elif not re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?', tag):
        raise ValueError('Invalid official release tag')
    request = urllib.request.Request(f'https://github.com/{repo}/releases/expanded_assets/{tag}', headers={'User-Agent': 'TelegramManager'})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Official asset metadata is too large')
    assets = []
    for row in re.findall(r'<li\b[^>]*>.*?</li>', raw.decode('utf-8'), re.S):
        links = re.findall(r'href="(/' + re.escape(repo) + r'/releases/download/' + re.escape(tag) + r'/[^"?]+)"', row)
        hashes = re.findall(r'value="(sha256:[0-9a-fA-F]{64})"', row)
        if len(links) == 1:
            assets.append({'name': links[0].split('/')[-1], 'browser_download_url': 'https://github.com' + links[0],
                           'digest': hashes[0] if len(hashes) == 1 else None})
    return {'tag_name': tag, 'assets': assets, 'prerelease': bool(re.search(r'[-.]', tag.lstrip('v').split('.', 2)[-1]))}


def official_release(client):
    repo = REPOSITORIES[client]
    request = urllib.request.Request(f'https://api.github.com/repos/{repo}/releases/latest',
                                     headers={'User-Agent': 'TelegramManager', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('Official release metadata is too large')
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429, 502, 503, 504):
            raise
        e.close()
        data = _official_release_page(repo)
    if not isinstance(data, dict):
        raise ValueError('Invalid official release metadata')
    tag = data.get('tag_name', '')
    if data.get('draft') or data.get('prerelease') or not re.fullmatch(r'v?\d+\.\d+\.\d+', tag):
        raise ValueError('No supported stable official release was found')
    version = tag.lstrip('v')
    pattern = r'(?:td-setup-mac-\d+\.\d+\.\d+|tsetup\.\d+\.\d+\.\d+)\.dmg' if client == 'Telegram' else r'AyuGram\.dmg'
    assets = [a for a in data.get('assets', []) if isinstance(a, dict) and re.fullmatch(pattern, a.get('name', ''))]
    if len(assets) != 1:
        raise ValueError('An unambiguous official macOS download was not found')
    asset = assets[0]
    url = f'https://github.com/{repo}/releases/download/{tag}/{asset["name"]}'
    if asset.get('browser_download_url') != url:
        raise ValueError('Unexpected official download URL')
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise ValueError('The official macOS download has no published SHA-256 checksum; installation is unavailable')
    return {'client': client, 'version': version, 'tag': tag, 'download_url': url,
            'sha256': digest.split(':')[1].lower(), 'release_url': f'https://github.com/{repo}/releases/tag/{tag}',
            'published_at': data.get('published_at', '')}


def check_releases():
    def check(client):
        current = installed_version(client)
        row = {'client': client, 'installed_version': current}
        if client == 'AyuGram' and inline_history_installed():
            row['installed_variant'] = 'Custom build with persistent inline deleted messages'
        try:
            release = official_release(client)
            key = _server()._version_tuple(current)
            row.update(release)
            row['update_available'] = not key or _server()._version_tuple(release['version']) > key
        except (OSError, ValueError, KeyError, TypeError) as e:
            row.update(error=f'Could not check official releases: {e}', update_available=False)
        return row
    with ThreadPoolExecutor(max_workers=2) as executor:
        clients = list(executor.map(check, REPOSITORIES))
    installed = {r['client']: r['installed_version'] for r in clients}
    return {'success': True, 'clients': clients,
            'warning': compatibility(installed['Telegram'], installed['AyuGram']),
            'note': 'Version checks do not certify session format compatibility. Test imports on a separate copy first.'}


def _shared_directory():
    directory = os.path.join(state.DATA_DIR, '_apps', 'macOS')
    for path in (os.path.join(state.DATA_DIR, '_apps'), directory):
        if os.path.islink(path) or not os.path.realpath(path).startswith(os.path.realpath(state.DATA_DIR) + os.sep):
            raise ValueError('Unsafe shared-app directory')
    os.makedirs(directory, exist_ok=True)
    return directory


def _ayu_paths():
    directory = _shared_directory()
    paths = [os.path.join(directory, name) for name in (
        'AyuGram.app', 'AyuGram.app.new', 'AyuGram.app.previous', '.ayugram-official.json', '.ayugram-update.json')]
    for path in paths + [paths[3] + '.tmp', paths[4] + '.tmp']:
        if os.path.islink(path) or (os.path.isfile(path) and os.stat(path).st_nlink > 1):
            raise ValueError('Unsafe AyuGram update path')
    return paths


def recover_ayugram_update():
    with INSTALL_LOCK:
        master, staging, previous, manifest, journal = _ayu_paths()
        if not os.path.isfile(journal):
            return
        with open(journal, encoding='utf-8') as f:
            recovery = json.load(f)
        if not isinstance(recovery, dict) or type(recovery.get('had_master')) is not bool:
            raise ValueError('Invalid AyuGram update recovery record')
        old_manifest = recovery.get('manifest')
        if old_manifest is not None and not isinstance(old_manifest, dict):
            raise ValueError('Invalid AyuGram installation manifest')
        if recovery['had_master'] and not os.path.isdir(previous) and not os.path.isdir(master):
            raise ValueError('The previous AyuGram app could not be recovered; installation stopped')
        if os.path.isdir(previous):
            if os.path.isdir(master):
                shutil.rmtree(master)
            os.rename(previous, master)
        elif not recovery['had_master'] and os.path.isdir(master):
            shutil.rmtree(master)
        if old_manifest is not None:
            state._save_json_atomic(manifest, old_manifest)
        elif os.path.exists(manifest):
            os.unlink(manifest)
        os.unlink(journal)


def _replace_ayugram(source, release):
    import ayugram
    import release_runtime
    release_root = release_runtime.state_dir(state.APP_BUNDLE)
    if (release_runtime.read(release_root / 'job.json').get('status') == 'queued'
            or release_runtime.read(release_root / 'transaction.json')):
        raise ValueError('Finish or cancel the scheduled managed update before replacing AyuGram')
    server = _server()
    recover_ayugram_update()
    master, staging, previous, manifest, journal = _ayu_paths()
    inline = release.get('kind') == 'local-inline'
    expected_id = ayugram.INLINE_BUNDLE_ID if inline else ayugram.BUNDLE_ID
    if server._bundle_identifier(source) != expected_id or server._bundle_version(source) != release['version']:
        raise ValueError('The AyuGram bundle does not match the selected installation')
    if inline:
        if (not os.path.isfile(ayugram._bundle_executable(source))
                or ayugram.bundle_fingerprint(source) != release.get('fingerprint')):
            raise ValueError('The local inline build does not match its pinned fingerprint')
        signed = subprocess.run(['codesign', '--verify', '--deep', '--strict', source],
                                capture_output=True, timeout=120)
        if signed.returncode:
            raise ValueError('The local inline build failed signature verification')
    current = server._version_tuple(server._bundle_version(master))
    if current and server._version_tuple(release['version']) < current:
        raise ValueError('AyuGram downgrade is not allowed')
    ok, error = server._copy_app_bundle(source, staging)
    if not ok:
        raise OSError(error)
    fingerprint = ayugram.bundle_fingerprint(staging)
    if fingerprint != ayugram.bundle_fingerprint(source):
        raise ValueError('The copied AyuGram bundle did not pass verification')
    old_manifest = None
    if os.path.isfile(manifest):
        with open(manifest, encoding='utf-8') as f:
            old_manifest = json.load(f)
        if not isinstance(old_manifest, dict):
            raise ValueError('Invalid existing AyuGram installation manifest')
    if os.path.isdir(previous):
        shutil.rmtree(previous)
    state._save_json_atomic(journal, {'had_master': os.path.isdir(master), 'manifest': old_manifest})
    try:
        if os.path.isdir(master):
            os.rename(master, previous)
        os.rename(staging, master)
        trusted = {'release': release['version'], 'fingerprint': fingerprint}
        if inline:
            trusted.update(kind='local-inline', bundle_id=expected_id,
                           source_commit=release.get('source_commit'), patch_sha256=release.get('patch_sha256'))
        else:
            trusted['sha256'] = release['sha256']
        state._save_json_atomic(manifest, trusted)
        os.unlink(journal)
    except (OSError, ValueError):
        recover_ayugram_update()
        raise


def _download(release, path):
    request = urllib.request.Request(release['download_url'], headers={'User-Agent': 'TelegramManager'})
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 600
    with urllib.request.urlopen(request, timeout=30) as response, open(path, 'wb') as target:
        final = urlparse(response.geturl())
        if final.scheme != 'https' or final.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
            raise ValueError('The official download redirected to an unexpected host')
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MAX_DOWNLOAD or time.monotonic() > deadline:
                raise ValueError('The official download exceeded the size or time limit')
            target.write(block)
            digest.update(block)
    if digest.hexdigest() != release['sha256']:
        raise ValueError('The downloaded package failed SHA-256 verification')


def _require_closed_accounts():
    server = _server()
    output = server._get_ps_output(force=True, allow_stale=False)
    if output is None:
        raise ValueError('Could not verify running clients; installation was stopped')
    if any(server._telegram_pid_from_ps_output(a['path'], output) for a in server.scan_accounts_cached()):
        raise ValueError('Quit all managed Telegram and AyuGram instances before installing an update')


def install_release(client, requested_version, acknowledgement=None):
    if not isinstance(client, str) or client not in REPOSITORIES or not isinstance(requested_version, str):
        return {'success': False, 'message': 'Invalid client or release version'}
    if not INSTALL_LOCK.acquire(blocking=False):
        return {'success': False, 'message': 'Another client update is already in progress'}
    mounted = False
    mount = None
    temporary = None
    installed = False
    try:
        if client == 'AyuGram':
            import release_runtime
            release_root = release_runtime.state_dir(state.APP_BUNDLE)
            if (release_runtime.read(release_root / 'job.json').get('status') == 'queued'
                    or release_runtime.read(release_root / 'transaction.json')):
                raise ValueError('Finish or cancel the scheduled managed update first')
            recover_ayugram_update()
        release = official_release(client)
        if release['version'] != requested_version:
            raise ValueError('The official release changed. Check for updates again before installing')
        current = _server()._version_tuple(installed_version(client))
        if current and _server()._version_tuple(requested_version) <= current:
            raise ValueError('This client is already at this version or newer; downgrade is not allowed')
        versions = {name: installed_version(name) for name in REPOSITORIES}
        versions[client] = requested_version
        warning = compatibility(versions['Telegram'], versions['AyuGram'])
        if client == 'AyuGram' and inline_history_installed():
            warning = (warning + ' ' if warning else '') + (
                'Installing the official release will replace the custom inline-history build. '
                'Saved history will remain, but deleted messages may no longer reappear in chats after restart.')
            versions['replaces_inline_history'] = True
        if warning and acknowledgement != versions:
            return {'success': False, 'confirmation_required': True, 'warning': warning,
                    'acknowledgement': versions, 'message': warning}
        _require_closed_accounts()
        directory = _shared_directory()
        temporary = tempfile.mkdtemp(prefix='client-update-', dir=os.path.dirname(directory))
        package = os.path.join(temporary, 'official.dmg')
        mount = os.path.join(temporary, 'mount')
        os.mkdir(mount)
        _download(release, package)
        mounted = True
        result = subprocess.run(['hdiutil', 'attach', package, '-readonly', '-nobrowse', '-noautoopen', '-mountpoint', mount],
                                capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError('Could not open the verified official disk image')
        try:
            source = os.path.join(mount, client + '.app')
            if os.path.islink(source) or _server()._bundle_version(source) != requested_version:
                raise ValueError('The app in the disk image does not match the selected release')
            _require_closed_accounts()
            if client == 'Telegram':
                ok, message = _server()._replace_shared_app(source)
                if not ok:
                    raise ValueError(message)
            else:
                _replace_ayugram(source, release)
            installed = True
        finally:
            result = subprocess.run(['hdiutil', 'detach', mount], capture_output=True, timeout=30)
            if result.returncode:
                raise ValueError('The update image could not be unmounted; check the shared app status before retrying')
            mounted = False
        _server().invalidate_scan_cache()
        return {'success': True, 'message': f'{client} {requested_version} installed. The previous app was retained. Account clones refresh on next open; sessions were not modified.'}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as e:
        state._log.warning('Client update failed for %s: %s', client, e)
        message = f'{client} {requested_version} was installed, but cleanup failed: {e}' if installed else str(e)
        return {'success': False, 'installed': installed, 'message': message}
    finally:
        if mounted and mount:
            try:
                detached = subprocess.run(['hdiutil', 'detach', mount], capture_output=True, timeout=30)
                mounted = detached.returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                pass
            if mounted:
                try:
                    info = subprocess.run(['hdiutil', 'info', '-plist'], capture_output=True, timeout=10)
                    images = plistlib.loads(info.stdout).get('images') if info.returncode == 0 else None
                    if isinstance(images, list):
                        mounted = any(os.path.realpath(entity.get('mount-point', '')) == os.path.realpath(mount)
                                      for image in images for entity in image.get('system-entities', []))
                except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
                    pass
        if temporary and not mounted:
            shutil.rmtree(temporary, ignore_errors=True)
        INSTALL_LOCK.release()


# Weekly metadata-only monitor. This never calls the installer or touches sessions.
_MONITOR_LOCK = threading.Lock()
_MONITOR_STATE_LOCK = threading.RLock()
_MONITOR_STOP = threading.Event()
_MONITOR_CHECKING = False
_MONITOR_ERROR = ''
_MONITOR_TZ = ZoneInfo('America/New_York')


def _monitor_paths():
    path = os.path.join(state.DATA_DIR, 'client_update_monitor.json')
    for candidate in (path, path + '.tmp'):
        if os.path.lexists(candidate):
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe client update monitor file')
    return path


def _read_monitor():
    path = _monitor_paths()
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError('Invalid client update monitor state')
    return value


def _save_monitor(value):
    path = _monitor_paths()
    if os.path.exists(path + '.tmp'):
        os.unlink(path + '.tmp')
    # Exclusive creation also prevents following a substituted temporary symlink.
    descriptor = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(value, target)
            target.flush()
            os.fsync(target.fileno())
        _monitor_paths()
        os.replace(path + '.tmp', path)
    finally:
        if os.path.exists(path + '.tmp') and not os.path.islink(path + '.tmp'):
            os.unlink(path + '.tmp')


def _weekly_slot(now):
    local = datetime.fromtimestamp(now, _MONITOR_TZ)
    slot = (local - timedelta(days=local.weekday())).replace(hour=10, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=7)
    return slot.timestamp(), (slot + timedelta(days=7)).timestamp()


def monitor_status():
    global _MONITOR_ERROR
    with _MONITOR_STATE_LOCK:
        try:
            value = _read_monitor()
        except (OSError, ValueError, TypeError) as error:
            value = {}
            _MONITOR_ERROR = str(error)
        fingerprint = value.get('notification_fingerprint', '')
        return {'last_checked': value.get('last_success', 0), 'next_check': _weekly_slot(time.time())[1],
                'checking': _MONITOR_CHECKING, 'notices': value.get('notices', []),
                'notification_fingerprint': fingerprint,
                'dismissed': bool(fingerprint and fingerprint == value.get('dismissed_fingerprint')),
                'error': _MONITOR_ERROR or value.get('error', ''),
                'error_notice': '', 'source_errors': value.get('source_errors', []),
                'candidates': value.get('candidates', [])}


def dismiss_monitor_notice(fingerprint):
    global _MONITOR_ERROR
    with _MONITOR_STATE_LOCK:
        try:
            value = _read_monitor()
            if isinstance(fingerprint, str) and fingerprint and fingerprint == value.get('notification_fingerprint'):
                value['dismissed_fingerprint'] = fingerprint
                _save_monitor(value)
                _MONITOR_ERROR = ''
                return True
        except (OSError, ValueError, TypeError) as error:
            _MONITOR_ERROR = str(error)
        return False


def _monitor_fetch(url, as_json=True):
    request = urllib.request.Request(url, headers={'User-Agent': 'TelegramManager', 'Accept': 'application/vnd.github+json' if as_json else '*/*'})
    with urllib.request.urlopen(request, timeout=15) as response:
        final = urlparse(response.geturl())
        expected = urlparse(url)
        if final.scheme != 'https' or final.hostname != expected.hostname or final.path != expected.path:
            raise ValueError('Unexpected official metadata redirect')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Official metadata is too large')
    return json.loads(raw) if as_json else raw.decode('utf-8')


def _ayu_candidate(release):
    repo = REPOSITORIES['AyuGram']
    tag = release.get('tag_name', '')
    if release.get('draft') or not re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?', tag):
        return None
    assets = [a for a in release.get('assets', []) if isinstance(a, dict) and a.get('name') == 'AyuGram.dmg']
    if not assets:
        return None
    if len(assets) != 1:
        raise ValueError('Ambiguous official AyuGram macOS assets')
    asset = assets[0]
    url = f'https://github.com/{repo}/releases/download/{tag}/AyuGram.dmg'
    digest = asset.get('digest', '')
    if asset.get('browser_download_url') != url or not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise ValueError('Unverified official AyuGram macOS asset')
    return {'client': 'AyuGram', 'version': tag.lstrip('v'), 'tag': tag, 'sha256': digest[7:].lower(),
            'release_url': f'https://github.com/{repo}/releases/tag/{tag}', 'prerelease': bool(release.get('prerelease'))}


def _candidate_evidence(candidate):
    repo = REPOSITORIES['AyuGram']
    ref = quote(candidate.get('commit') or candidate['tag'], safe='')
    root = f'https://raw.githubusercontent.com/{repo}/{ref}/Telegram/SourceFiles/'
    version = _monitor_fetch(root + 'core/version.h', False)
    storage = _monitor_fetch(root + 'storage/details/storage_file_utilities.cpp', False)
    match = re.search(r'\bAppVersion\s*=\s*(\d+)', version)
    result = {'source_url': root + 'core/version.h', 'reader_url': root + 'storage/details/storage_file_utilities.cpp'}
    if match:
        result['app_version'] = int(match.group(1))
    # Reader code is evidence to review, never a compatibility certification.
    result['reader_rejects_newer'] = bool(re.search(r'\bversion\s*>\s*(?:(?:[A-Za-z_]\w*)::)*AppVersion\b', storage))
    return result


def _monitor_version(version):
    match = re.match(r'^v?(\d+)\.(\d+)\.(\d+)(?:$|[-.])', version or '')
    return tuple(map(int, match.groups())) if match else ()


def _extra_ayu_candidates(installed=None, known=None):
    repo = REPOSITORIES['AyuGram']
    root = f'https://api.github.com/repos/{repo}'
    candidates, errors = [], []
    known = known or {}
    releases = []
    try:
        try:
            releases = _monitor_fetch(root + '/releases?per_page=30')
        except urllib.error.HTTPError as error:
            if error.code not in (403, 429):
                raise
            error.close()
            page = _monitor_fetch(f'https://github.com/{repo}/releases', False)
            tags = set(re.findall(r'href=[\"\'](?:https://github\.com)?/' + re.escape(repo) + r'/releases/tag/(v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?)[\"\']', html.unescape(page)))
            tags = sorted((tag for tag in tags if _monitor_version(tag) >= _monitor_version(installed)), key=_monitor_version, reverse=True)
            for tag in tags[:5]:
                cached = next((c for c in reversed(list(known.values())) if c.get('client') == 'AyuGram' and c.get('tag') == tag), None)
                if cached:
                    candidates.append(dict(cached))
                else:
                    releases.append(_official_release_page(repo, tag))
        if not isinstance(releases, list):
            raise ValueError('Invalid official AyuGram releases list')
        for release in releases[:30]:
            if not isinstance(release, dict):
                continue
            try:
                candidate = _ayu_candidate(release)
                if candidate:
                    candidates.append(candidate)
            except ValueError as error:
                errors.append(str(error))
    except (OSError, ValueError, TypeError) as error:
        errors.append(f'AyuGram release metadata: {error}')
    try:
        data = _monitor_fetch(root + '/actions/artifacts?per_page=30')
        if not isinstance(data, dict) or not isinstance(data.get('artifacts'), list):
            raise ValueError('Invalid official build metadata')
        for artifact in data['artifacts'][:30]:
            if not isinstance(artifact, dict):
                continue
            run = artifact.get('workflow_run') or {}
            if not isinstance(run, dict):
                continue
            commit = run.get('head_sha', '')
            if (artifact.get('expired') or not re.search(r'(?:macos|darwin|(?:^|[^a-z])mac(?:[^a-z]|$))', artifact.get('name', ''), re.I)
                    or not re.fullmatch(r'[0-9a-fA-F]{40}', commit)
                    or run.get('head_repository_id') != run.get('repository_id')
                    or not run.get('repository_id') or not isinstance(run.get('id'), int)):
                continue
            candidates.append({'client': 'AyuGram', 'tag': commit, 'commit': commit, 'version': '', 'build': True,
                               'sha256': artifact.get('digest', ''),
                               'release_url': f'https://github.com/{repo}/actions/runs/{run["id"]}'})
    except urllib.error.HTTPError as error:
        if error.code != 404:
            errors.append(f'AyuGram public macOS build metadata unavailable: {error}')
        error.close()
    except (OSError, ValueError, TypeError, AttributeError) as error:
        errors.append(f'AyuGram public macOS build metadata unavailable: {error}')
    known = known or {}
    try:
        channel = _monitor_fetch('https://t.me/s/AyuGramReleases', False)
        links = set(re.findall(r'https://github\.com/' + re.escape(repo) + r'/releases/tag/(v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?)', html.unescape(channel)))
        for tag in sorted(links)[:10]:
            if (_monitor_version(tag) <= _monitor_version(installed)
                    or any(c['tag'] == tag for c in candidates)
                    or any(c.get('client') == 'AyuGram' and c.get('tag') == tag for c in known.values())):
                continue
            try:
                try:
                    release = _monitor_fetch(root + '/releases/tags/' + quote(tag, safe=''))
                except urllib.error.HTTPError as error:
                    if error.code not in (403, 429):
                        raise
                    error.close()
                    release = _official_release_page(repo, tag)
                candidate = _ayu_candidate(release)
                if candidate:
                    candidates.append(candidate)
            except (OSError, ValueError, TypeError) as error:
                errors.append(f'AyuGram linked GitHub release metadata: {error}')
    except (OSError, ValueError, TypeError, AttributeError) as error:
        errors.append(f'AyuGram official channel: {error}')
    installed = _monitor_version(installed)
    known = known or {}
    latest = max((_monitor_version(c.get('version')) for c in candidates if not c.get('build')), default=())
    relevant = [c for c in candidates if c.get('build') or _monitor_version(c.get('version')) > installed
                or _monitor_version(c.get('version')) == latest]
    relevant.sort(key=lambda c: (c.get('build', False), _monitor_version(c.get('version'))), reverse=True)
    fetched = 0
    for candidate in relevant:
        cached = known.get(_candidate_id(candidate), {}).get('evidence')
        if cached:
            candidate['evidence'] = cached
            continue
        if fetched >= 5:  # Two source files per candidate: at most ten source requests.
            break
        fetched += 1
        try:
            candidate['evidence'] = _candidate_evidence(candidate)
        except (OSError, ValueError, TypeError) as error:
            errors.append(f'AyuGram source evidence for {candidate["tag"]}: {error}')
    return candidates, errors


def _candidate_id(candidate):
    return hashlib.sha256(json.dumps([candidate['client'], candidate.get('tag'), candidate.get('sha256')], separators=(',', ':')).encode()).hexdigest()


def run_weekly_check(now=None):
    global _MONITOR_CHECKING, _MONITOR_ERROR
    now = time.time() if now is None else float(now)
    if not _MONITOR_LOCK.acquire(blocking=False):
        return monitor_status()
    try:
        with _MONITOR_STATE_LOCK:
            value = _read_monitor()
            if value.get('last_success', 0) >= _weekly_slot(now)[0]:
                return monitor_status()
            attempt = value.get('last_attempt')
            if attempt is not None and now - attempt < 3600:
                return monitor_status()
            value['last_attempt'] = now
            _save_monitor(value)
            _MONITOR_CHECKING = True
        result = check_releases()
        current = {row['client']: row.get('installed_version') for row in result['clients']}
        known = {_candidate_id(c): c for c in value.get('candidates', [])}
        for candidate in result['clients']:
            if not candidate.get('error'):
                known.setdefault(_candidate_id(candidate), candidate)
        extra, source_errors = _extra_ayu_candidates(current.get('AyuGram'), known)
        basic = [row for row in result['clients'] if not row.get('error')]
        errors = [row['error'] for row in result['clients'] if row.get('error')]
        evidence = {_candidate_id(candidate): candidate.get('evidence') for candidate in extra}
        for candidate in basic:
            if evidence.get(_candidate_id(candidate)):
                candidate['evidence'] = evidence[_candidate_id(candidate)]
        candidates = basic + extra
        with _MONITOR_STATE_LOCK:
            # Dismissal can happen while network reads are in flight.
            value = _read_monitor()
            seen = set(value.get('seen', []))
            notices = list(value.get('notices', []))
            previously_dismissed = bool(value.get('notification_fingerprint') and value.get('notification_fingerprint') == value.get('dismissed_fingerprint'))
            old_candidates = {_candidate_id(c): c for c in value.get('candidates', [])}
            for candidate in candidates:
                identity = _candidate_id(candidate)
                version = _monitor_version(candidate.get('version'))
                installed = _monitor_version(current.get(candidate['client']))
                available = (bool(value.get('last_success')) if candidate.get('build') else not installed or version > installed)
                replacement = any(c.get('client') == candidate['client'] and c.get('tag') == candidate.get('tag') and c.get('sha256') != candidate.get('sha256') for c in old_candidates.values())
                if identity not in seen and (available or replacement):
                    if previously_dismissed:
                        notices = []
                        previously_dismissed = False
                    label = ('macOS build ' + candidate['commit'][:12]) if candidate.get('build') else candidate.get('version', '')
                    message = f'New {candidate["client"]} {label} metadata available. Session compatibility is unverified; test on a fresh separate copy first.'
                    evidence = candidate.get('evidence', {})
                    if evidence.get('app_version', 7003001) < 7003001:
                        message += ' Source AppVersion is below 7003001; this candidate cannot read sessions requiring that version.'
                    notices.append({'message': message, 'url': candidate['release_url'], 'evidence': evidence, 'fingerprint': identity})
                seen.add(identity)
                old_candidates[identity] = candidate
            value.update(candidates=list(old_candidates.values()), seen=sorted(seen), notices=notices,
                         source_errors=source_errors, error='; '.join(errors))
            value['notification_fingerprint'] = hashlib.sha256(json.dumps(notices, sort_keys=True).encode()).hexdigest() if notices else ''
            if not errors and not source_errors:
                value['last_success'] = now
            _save_monitor(value)
            _MONITOR_ERROR = ''
    except (OSError, ValueError, TypeError, KeyError) as error:
        _MONITOR_ERROR = str(error)
    finally:
        _MONITOR_CHECKING = False
        _MONITOR_LOCK.release()
    return monitor_status()


def run_weekly_monitor():
    while not _MONITOR_STOP.is_set():
        run_weekly_check()
        try:
            import managed_releases
            managed_releases.run_scheduled_check()
            managed_releases.ensure_worker()
        except (OSError, ValueError, KeyError, TypeError) as error:
            state._log.warning('Managed release check failed: %s', error)
        _MONITOR_STOP.wait(60)
