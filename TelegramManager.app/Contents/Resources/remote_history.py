"""Opt-in archive synchronization. Authentication and settings are never transferred."""
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid

import history_archive
import remote_ayugram
import state

INTERVAL_SECONDS = 300
_lock = threading.RLock()  # Configuration only; never held across network/account locks.
_transfer = threading.Lock()
_MARKER = b'ayugram-inline-isolated-profile-v1\n'
_ERRORS = (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError)
_WAITING = 'Waiting for VPS history. Sync will start when saved history for the confirmed account ID appears.'


class Deferred(ValueError):
    pass


def _unlocked():
    import server
    return server._is_unlocked_no_touch()


def _regular(path, private=False):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or (private and info.st_mode & 0o077)):
        raise ValueError('History sync requires private, unlinked regular files.')
    return info


def _config_path():
    directory = Path(state.DATA_DIR)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('History sync configuration directory is unsafe.')
    return directory / 'remote_history.json'


def _load():
    path = _config_path()
    if not os.path.lexists(path):
        return {'schema': 1, 'bindings': {}}
    if _regular(path, private=True).st_size > 1_048_576:
        raise ValueError('History sync configuration exceeds its size limit.')
    with open(path, encoding='utf-8') as stream:
        config = json.load(stream)
    if (not isinstance(config, dict) or set(config) != {'schema', 'bindings'}
            or type(config['schema']) is not int or config['schema'] != 1
            or not isinstance(config['bindings'], dict) or len(config['bindings']) > 1000):
        raise ValueError('Invalid history sync configuration.')
    for account, binding in config['bindings'].items():
        if (not isinstance(account, str) or not os.path.isabs(account) or not isinstance(binding, dict)
                or not isinstance(binding.get('id'), str)
                or str(uuid.UUID(binding['id'])) != binding['id']
                or not _valid_id(binding.get('user_id')) or type(binding.get('enabled')) is not bool
                or type(binding.get('awaiting_history', False)) is not bool
                or not isinstance(binding.get('remote'), dict)):
            raise ValueError('Invalid history sync binding.')
        for field in ('last_attempt', 'last_success'):
            if type(binding.get(field, 0)) not in (int, float) or not 0 <= binding.get(field, 0) < 1e12:
                raise ValueError('Invalid history sync timestamp.')
    return config


def _save(config):
    path = _config_path()
    if os.path.lexists(path):
        _regular(path, private=True)
    fd, temporary = tempfile.mkstemp(prefix='.remote-history-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, ensure_ascii=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,18}', value) is not None and int(value) < 2**63


def _target(account):
    if not isinstance(account, str) or not os.path.isabs(account) or not state.is_managed_account_path(account):
        raise ValueError('Select an existing managed account with its own custom AyuGram profile.')
    account = os.path.abspath(account)
    base = Path(account)
    portable = base / '.ayugram/TelegramForcePortable'
    for directory in (base / '.ayugram', portable, portable / 'tdata'):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('The local AyuGram profile is missing or contains a linked directory.')
    marker = portable / '.ayugram-inline-test'
    if _regular(marker).st_size != len(_MARKER) or marker.read_bytes() != _MARKER:
        raise ValueError('Open this profile with the custom AyuGram build before enabling history sync.')
    archive = portable / 'tdata/ayudata.db'
    _regular(archive)
    return account, archive


def _archive_in_use(path):
    result = subprocess.run(['/usr/sbin/lsof', '-t', '--', str(path)],
                            capture_output=True, text=True, timeout=5)
    lines = result.stdout.splitlines()
    if result.returncode not in (0, 1) or result.stderr.strip() or any(not line.isdigit() for line in lines):
        raise ValueError('Could not verify that the local history archive is closed.')
    return any(int(pid) != os.getpid() for pid in lines)


def _check_closed(account, archive):
    import server
    if not _unlocked():
        raise Deferred('History sync waits until Manager is unlocked.')
    output = server._get_ps_output(force=True, allow_stale=False)
    if output is None:
        raise ValueError('Could not check running apps. History sync was not applied.')
    if server._client_pid_from_ps_output(account, output, 'AyuGram') or _archive_in_use(archive):
        raise Deferred('Waiting for local AyuGram to close. Nothing was force-closed.')
    # Also catch a direct launch of the shared executable with this workdir.
    workdir = str(archive.parent.parent)
    if any('-workdir ' in line and workdir in line for line in output.splitlines()):
        raise Deferred('Waiting for local AyuGram to close. Nothing was force-closed.')
    return True


def _accounts(reply):
    if (not isinstance(reply, dict) or reply.get('protocol') != 1 or reply.get('schema_version') != 1
            or not isinstance(reply.get('accounts'), list) or len(reply['accounts']) > 1000):
        raise ValueError('Unsupported history catalog. Update the Manager and VPS helper together.')
    accounts = []
    for item in reply['accounts']:
        if (not isinstance(item, dict) or not _valid_id(item.get('user_id'))
                or any(type(item.get(key)) is not int or not 0 <= item[key] <= 100_000_000
                       for key in ('deleted', 'edited'))):
            raise ValueError('Invalid history account catalog.')
        accounts.append({key: item[key] for key in ('user_id', 'deleted', 'edited')})
    return accounts


def _public(path, binding):
    fields = ('id', 'user_id', 'enabled', 'state', 'message', 'last_attempt', 'last_success', 'counts')
    return {'path': path, **{key: copy.deepcopy(binding[key]) for key in fields if key in binding}}


def status():
    try:
        with _lock:
            config = _load()
        return {'success': True, 'interval_seconds': INTERVAL_SECONDS,
                'bindings': [_public(path, binding) for path, binding in config['bindings'].items()]}
    except _ERRORS:
        return {'success': False, 'bindings': [], 'message': 'Could not read the private history sync configuration.'}


def _record(account, identity, **changes):
    with _lock:
        config = _load()
        binding = config['bindings'].get(account)
        if binding and binding['id'] == identity:
            binding.update(changes)
            _save(config)


def _sync(account, archive):
    with _lock:
        binding = copy.deepcopy(_load()['bindings'].get(account))
    if not binding or not binding['enabled']:
        raise ValueError('History sync is not enabled for this profile.')
    identity = binding['id']
    try:
        _check_closed(account, archive)
        if remote_ayugram._config() != binding['remote']:
            raise ValueError('The VPS connection changed. Confirm the history binding again before syncing.')
        local = _accounts(history_archive.catalog(archive))
        if binding['user_id'] not in {item['user_id'] for item in local}:
            raise ValueError('The bound account ID is missing from the local archive. Confirm the correct profile.')
        if not _transfer.acquire(blocking=False):
            raise Deferred('Another history transfer is in progress. This profile will retry automatically.')
        try:
            _record(account, identity, state='syncing', message='Fetching the saved VPS history.', last_attempt=time.time())
            if binding.get('awaiting_history', False):
                remote = _accounts(remote_ayugram.history_request(
                    'history-catalog', {}, expected_config=binding['remote']))
                if not remote:
                    _record(account, identity, state='waiting', message=_WAITING)
                    return {'success': True, 'state': 'waiting', 'message': _WAITING}
                if binding['user_id'] not in {item['user_id'] for item in remote}:
                    raise ValueError('The VPS history belongs to a different account ID. Nothing was imported.')
            known = history_archive.imported_digest(archive, identity, binding['user_id'])
            envelope = remote_ayugram.history_request('history-export',
                        {'user_id': binding['user_id'], 'known_digest': known}, expected_config=binding['remote'])
            if not isinstance(envelope, dict) or envelope.get('user_id') != binding['user_id']:
                raise ValueError('The VPS returned history for a different account. Nothing was imported.')
            _check_closed(account, archive)
            backup = Path(state.DATA_DIR) / 'History Sync Backups' / (identity + '.db')
            result = history_archive.merge_archive(archive, envelope, source_id=identity, backup_path=backup,
                                                   check_closed=lambda: _check_closed(account, archive))
        finally:
            _transfer.release()
        counts = {key: result[key] for key in ('imported', 'skipped', 'conflicts', 'ambiguities')}
        # An unchanged source must not erase the preceding import's review notice.
        if result['unchanged'] and binding.get('counts'):
            counts = binding['counts']
        warning = counts['conflicts'] or counts['ambiguities']
        message = ('History synced. Existing local content was kept; review the conflict and ordering counts.' if warning
                   else 'History is up to date. Open local AyuGram normally to view it.')
        _record(account, identity, state='warning' if warning else 'ready', message=message,
                last_success=time.time(), counts=counts, awaiting_history=False)
        return {'success': True, 'state': 'warning' if warning else 'ready', 'message': message, **result}
    except _ERRORS as error:
        deferred = isinstance(error, Deferred)
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'History sync failed safely. Check archive access and VPS availability.'
        _record(account, identity, state='deferred' if deferred else 'error', message=message, last_attempt=time.time())
        return {'success': deferred, 'state': 'deferred' if deferred else 'error', 'message': message}


def request(action, payload=None):
    payload = payload or {}
    try:
        if not isinstance(payload, dict):
            raise ValueError('Invalid history sync request.')
        if action not in ('catalog', 'configure', 'sync', 'disable'):
            raise ValueError('Unsupported history sync action.')
        if not _unlocked():
            raise ValueError('Unlock Manager before changing or syncing history.')
        path = payload.get('path')
        # Disabling remains possible if the account was moved or removed.
        if action == 'disable':
            if not isinstance(path, str) or not os.path.isabs(path):
                raise ValueError('Select a configured history binding.')
            account, archive = os.path.abspath(path), None
        else:
            account, archive = _target(path)
        lock = state._account_path_lock(account)
        if not lock.acquire(blocking=False):
            return {'success': True, 'state': 'deferred', 'message': 'This profile is busy. Try again after its current operation.'}
        try:
            if action == 'disable':
                with _lock:
                    config = _load()
                    binding = config['bindings'].get(account)
                    if binding:
                        binding.update(enabled=False, state='disabled', message='History sync disabled. Saved history is preserved.')
                        _save(config)
                return {'success': True, 'message': 'History sync disabled. Saved history is preserved.'}
            if action == 'sync':
                return _sync(account, archive)
            _check_closed(account, archive)
            remote_config = remote_ayugram._config()
            if remote_config is None:
                raise ValueError('Configure Remote AyuGram before enabling history sync.')
            if action == 'configure' and (payload.get('confirmed') is not True or not _valid_id(payload.get('user_id'))):
                raise ValueError('Confirm that the numeric account ID belongs to both independent logins.')
            local = _accounts(history_archive.catalog(archive))
            if not _transfer.acquire(blocking=False):
                raise Deferred('Another history transfer is in progress. Try again shortly.')
            try:
                remote = _accounts(remote_ayugram.history_request('history-catalog', {}, expected_config=remote_config))
            finally:
                _transfer.release()
            if action == 'catalog':
                return {'success': True, 'local': local, 'remote': remote,
                        'message': 'Archive IDs identify saved history, not the current login. Confirm the same account in both clients yourself.'}
            user_id = payload['user_id']
            if user_id not in {item['user_id'] for item in local}:
                raise ValueError('That account ID must already appear in the local history archive. No sessions were changed.')
            if remote and user_id not in {item['user_id'] for item in remote}:
                raise ValueError('The VPS history belongs to a different account ID. Nothing was imported.')
            _check_closed(account, archive)
            with _lock:
                config = _load()
                old = config['bindings'].get(account)
                same = old and old['user_id'] == user_id and old['remote'] == remote_config
                binding = copy.deepcopy(old) if same else {'id': str(uuid.uuid4()), 'user_id': user_id,
                    'remote': remote_config, 'last_attempt': 0, 'last_success': 0}
                binding.update(enabled=True, awaiting_history=not remote,
                               state='idle' if remote else 'waiting',
                               message='Enabled. History will sync while local AyuGram is closed.' if remote else _WAITING)
                config['bindings'][account] = binding
                _save(config)
            return {'success': True, 'message': binding['message'], 'binding': _public(account, binding)}
        finally:
            lock.release()
    except _ERRORS as error:
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'Could not access the history archive or private sync configuration.'
        # Missing/moved profiles fail before _sync can persist the error. Surface
        # those failures to the monitor too, without following the missing path.
        if action == 'sync' and isinstance(payload, dict) and isinstance(payload.get('path'), str):
            try:
                with _lock:
                    config = _load()
                    binding = config['bindings'].get(os.path.abspath(payload['path']))
                    if binding and binding['enabled']:
                        binding.update(state='error', message=message, last_attempt=time.time())
                        _save(config)
            except _ERRORS:
                pass
        return {'success': False, 'message': message}


def before_open(account):
    """Called under the existing account RLock; failures do not erase/block local history."""
    snapshot = status()
    if not snapshot['success']:
        return snapshot['message']
    if not any(item['path'] == os.path.abspath(account) and item['enabled'] for item in snapshot['bindings']):
        return None
    result = request('sync', {'path': account})
    if not result['success'] or result.get('state') == 'deferred':
        return 'VPS history sync: ' + result['message']
    return None


def run_pass():
    if not _unlocked():
        return
    now = time.time()
    for binding in status()['bindings']:
        if not _unlocked():
            break
        attempted = binding.get('last_attempt', 0)
        if binding['enabled'] and (now < attempted or now - attempted >= INTERVAL_SECONDS):
            request('sync', {'path': binding['path']})


def run_monitor():
    while True:
        try:
            run_pass()
        except _ERRORS:
            state._log.warning('History sync monitor could not access its configuration.')
        time.sleep(30)
