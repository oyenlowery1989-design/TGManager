"""Selective, path-independent Mac transfer. Never read or copy login files.

Only explicit UI requests call this module. History uses the same bounded,
text-only interchange as VPS sync. No client is opened, stopped or contacted.
"""
import base64
import copy
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import uuid

import ayugram
import history_archive
import remote_history
import state

FORMAT = 'telegram-manager-selective-transfer'
MAX_BYTES = 64 * 1024 * 1024
MAX_ACCOUNTS = 1000
_transfer_lock = threading.Lock()
_PARTS = {'history', 'settings', 'organization'}
_META_STRINGS = {'notes': 100_000, 'usernames': 512, 'colors': 9, 'dock_names': 255}
_PINS = ('pinned', 'folder_pinned')
_BOOL_PREFS = {'saveDeletedMessages', 'saveMessagesHistory', 'useGlobalGhostMode',
               'semiTransparentDeletedMessages'}
_GHOST_PREFS = set(ayugram.COMMON_PROFILE['ghostModeSettings']['0'])
_PREFS = _BOOL_PREFS | {'ghostModeSettings', 'translationProvider', 'showPeerId'}
_ERRORS = (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('Invalid ' + label + '.')


def _string(value, limit, label):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8')
    return value


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid transfer identifier.')
    return value


def _include(value):
    _object(value, _PARTS, 'transfer selection')
    if any(type(v) is not bool for v in value.values()) or not any(value.values()):
        raise ValueError('Select at least one type of data to transfer.')
    return value


def _preferences(value, *, strict=True):
    if not isinstance(value, dict) or (strict and set(value) - _PREFS):
        raise ValueError('Unsupported AyuGram preferences.')
    result = {key: copy.deepcopy(v) for key, v in value.items() if key in _PREFS}
    for key, val in result.items():
        if key in _BOOL_PREFS and type(val) is not bool:
            raise ValueError('Invalid AyuGram preference type.')
        if key == 'showPeerId' and (type(val) is not int or not 0 <= val <= 2):
            raise ValueError('Invalid ID display preference.')
        if key == 'translationProvider' and (not isinstance(val, str)
                or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', val)):
            raise ValueError('Invalid translation provider.')
        if key == 'ghostModeSettings':
            if not isinstance(val, dict) or (strict and set(val) - {'0'}):
                raise ValueError('Only global Ghost Mode preferences can be transferred.')
            ghost = val.get('0', {})
            if not isinstance(ghost, dict) or (strict and set(ghost) - _GHOST_PREFS):
                raise ValueError('Invalid global Ghost Mode preferences.')
            ghost = {k: v for k, v in ghost.items() if k in _GHOST_PREFS}
            if any(type(v) is not bool for v in ghost.values()):
                raise ValueError('Invalid global Ghost Mode preference type.')
            result[key] = {'0': ghost} if ghost else {}
    return result


def _metadata(value):
    if not isinstance(value, dict) or set(value) - (set(_META_STRINGS) | set(_PINS)):
        raise ValueError('Unsupported account organization data.')
    for key, val in value.items():
        if key in _META_STRINGS:
            _string(val, _META_STRINGS[key], 'account ' + key)
            if key == 'colors' and val and not re.fullmatch('#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})', val):
                raise ValueError('Invalid account color.')
        elif type(val) is not bool:
            raise ValueError('Invalid account pin.')
    return value


def _package(value):
    _object(value, {'format', 'version', 'id', 'created', 'include', 'accounts',
                    'workspaces', 'digest'}, 'transfer package')
    if value['format'] != FORMAT or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported transfer format or version.')
    if len(_json(value)) > MAX_BYTES:
        raise ValueError('Transfer exceeds 64 MiB. Export fewer accounts at a time.')
    _uuid(value['id'])
    _string(value['created'], 64, 'creation time')
    parts = _include(value['include'])
    digest = value['digest']
    if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
            or not hmac.compare_digest(digest, hashlib.sha256(_json(
                {k: v for k, v in value.items() if k != 'digest'})).hexdigest())):
        raise ValueError('Transfer checksum does not match. Copy the original export again.')
    entries = value['accounts']
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ACCOUNTS:
        raise ValueError('Invalid transfer account list.')
    ids = set()
    for entry in entries:
        _object(entry, {'id', 'name', 'folder', 'metadata', 'settings', 'history'}, 'transfer account')
        identity = _uuid(entry['id'])
        if identity in ids:
            raise ValueError('Duplicate transfer account identifier.')
        ids.add(identity)
        _string(entry['name'], 255, 'account name')
        _string(entry['folder'], 4096, 'folder label')
        _metadata(entry['metadata'])
        if not parts['organization'] and entry['metadata']:
            raise ValueError('Unexpected organization data.')
        if entry['settings'] is not None:
            if not parts['settings']:
                raise ValueError('Unexpected preferences.')
            _preferences(entry['settings'])
        if entry['history'] is not None:
            if (not isinstance(entry['history'], dict) or not parts['history']
                    or entry['history'].get('unchanged') is not False):
                raise ValueError('Expected a complete history snapshot.')
            history_archive._envelope(entry['history'])
    workspaces = value['workspaces']
    if (not isinstance(workspaces, list) or len(workspaces) > MAX_ACCOUNTS
            or (not parts['organization'] and workspaces)):
        raise ValueError('Invalid transfer workspaces.')
    names = set()
    for item in workspaces:
        _object(item, {'name', 'icon', 'accounts'}, 'transfer workspace')
        name = _string(item['name'], 255, 'workspace name')
        _string(item['icon'], 64, 'workspace icon')
        members = item['accounts']
        if (not name or name in names or not isinstance(members, list)
                or len(members) > MAX_ACCOUNTS or any(not isinstance(m, str) for m in members)
                or len(set(members)) != len(members) or set(members) - ids):
            raise ValueError('Invalid workspace membership.')
        names.add(name)
    return value


def _account(path):
    if (not isinstance(path, str) or not os.path.isabs(path)
            or not state.is_managed_account_path(path)):
        raise ValueError('Choose an existing account managed by this Mac.')
    account = os.path.abspath(path)
    # Reject links in scan roots too, not just inside the account.
    _safe_file(Path(account) / '.transfer-path-check', exists=False)
    return account


def _safe_file(path, exists=True):
    identity = history_archive._safe_file(path, exists=exists)
    if identity and os.stat(path).st_uid != os.getuid():
        raise ValueError('Transfer files must belong to the current user.')
    return identity


def _read_json(path, *, optional=False):
    if _safe_file(path, exists=not optional) is None:
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('Preferences exceed the transfer size limit.')
    with open(path, encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError('Invalid preferences file. Existing data was preserved.')
    return value


def _profile(account):
    account, archive = remote_history._target(account)
    prefs = archive.with_name('ayu_settings.json')
    marker = Path(account) / '.ayugram/.common-profile.json'
    _safe_file(prefs, exists=False)
    _safe_file(marker, exists=False)
    return archive, prefs, marker


def _closed(account, archive):
    remote_history._check_closed(account, archive)
    # External processes opening settings must also be absent, even if the DB
    # itself is momentarily closed by the client.
    prefs = archive.with_name('ayu_settings.json')
    if prefs.exists() and remote_history._archive_in_use(prefs):
        raise ValueError('Quit local AyuGram normally before transferring preferences.')
    return True


def _label(account):
    roots = [state.ROOT_DIR] + state.config.get('extra_scan_dirs', [])
    root = next((os.path.abspath(os.path.expanduser(r)) for r in roots
                 if account.startswith(os.path.abspath(os.path.expanduser(r)) + os.sep)), None)
    relative = os.path.relpath(os.path.dirname(account), root) if root else ''
    return '' if relative == '.' else relative


def _catalog(account):
    result = dict(success=True, name=os.path.basename(account), folder=_label(account),
                  owners=[], settings_available=False, history_available=False)
    if not (Path(account) / '.ayugram').exists():
        return result
    archive, prefs, _ = _profile(account)
    _closed(account, archive)
    result['settings_available'] = prefs.exists()
    try:
        result.update(owners=history_archive.catalog(archive)['accounts'], history_available=True)
    except (ValueError, sqlite3.Error) as error:
        # A settings-only export must remain possible if an archive is too
        # large or uses a future schema. History export still fails explicitly.
        result['message'] = 'History cannot be transferred: ' + _message(error)
    _closed(account, archive)
    return result


def _locks(stack, accounts):
    for account in sorted(accounts):
        lock = state._account_path_lock(account)
        if not lock.acquire(blocking=False):
            raise ValueError('An account is busy. Retry after its current operation.')
        stack.callback(lock.release)


def _export(payload):
    parts = _include(payload.get('include'))
    selected = payload.get('accounts')
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_ACCOUNTS:
        raise ValueError('Select accounts to export.')
    paths = []
    for item in selected:
        _object(item, {'path', 'user_id'}, 'export account selection')
        paths.append(_account(item['path']))
    if len(set(paths)) != len(paths):
        raise ValueError('Select each account only once.')
    package = dict(format=FORMAT, version=1, id=str(uuid.uuid4()),
                   created=datetime.now(timezone.utc).isoformat(), include=dict(parts),
                   accounts=[], workspaces=[])
    with ExitStack() as stack:
        _locks(stack, paths)
        with state._meta_lock, state._ws_lock:
            metadata = copy.deepcopy(state.metadata)
            workspaces = state.load_workspaces() if parts['organization'] else {}
        identities = {}
        for account, selection in zip(paths, selected):
            _account(account)
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, FORMAT + ':' + account))
            identities[account] = identity
            entry = dict(id=identity, name=os.path.basename(account), folder=_label(account),
                         metadata={}, settings=None, history=None)
            if parts['organization']:
                entry['metadata'] = {key: metadata.get(key, {}).get(account, '') for key in _META_STRINGS}
                entry['metadata'].update({key: account in metadata.get(key, []) for key in _PINS})
            if (parts['history'] or parts['settings']) and (Path(account) / '.ayugram').exists():
                archive, prefs, _ = _profile(account)
                _closed(account, archive)
                if parts['history']:
                    owners = history_archive.catalog(archive)['accounts']
                    if owners:
                        if not remote_history._valid_id(selection['user_id']):
                            raise ValueError('Select the numeric archive account ID for each exported history.')
                        entry['history'] = history_archive.export_archive(archive, selection['user_id'])
                if parts['settings'] and prefs.exists():
                    entry['settings'] = _preferences(_read_json(prefs), strict=False)
                _closed(account, archive)
            package['accounts'].append(entry)
            if len(_json(package)) > MAX_BYTES - 128:
                raise ValueError('Transfer exceeds 64 MiB. Export fewer accounts at a time.')
        for name, workspace in workspaces.items():
            members = list(dict.fromkeys(identities[p] for p in workspace.get('accounts', []) if p in identities))
            if members:
                package['workspaces'].append(dict(name=name, icon=workspace.get('icon', '📁'), accounts=members))
        package['digest'] = hashlib.sha256(_json(package)).hexdigest()
        _package(package)
    return dict(success=True, package=package)


def _preview(package):
    rows = []
    for entry in package['accounts']:
        history = entry['history'] or {}
        rows.append(dict(id=entry['id'], name=entry['name'], folder=entry['folder'],
                         user_id=history.get('user_id', ''), deleted=len(history.get('deleted', [])),
                         edited=len(history.get('edited', [])), settings_count=len(entry['settings'] or {})))
    return dict(success=True, accounts=rows, workspaces=package['workspaces'], include=package['include'])


def _backup_path(name):
    directory = Path(state.DATA_DIR) / 'Mac Transfer Backups'
    _safe_file(directory / '.path-check', exists=False)
    if not directory.exists():
        directory.mkdir(mode=0o700)
    if not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError('Mac Transfer Backups must be a private directory.')
    result = directory / name
    _safe_file(result, exists=False)
    return result


def _atomic_bytes(path, content):
    _safe_file(path, exists=False)
    fd, temporary = tempfile.mkstemp(prefix='.mac-transfer-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_file(path, exists=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_files(files, backup_name, check):
    """Private recovery copy + atomic file writes; rollback normal I/O failures.

    Recovery copy is retained for interruption/power loss between file renames.
    This is intentionally separate from the SQLite history transaction.
    """
    changes = {}
    before = {}
    for path, value in files.items():
        _safe_file(path, exists=False)
        old = path.read_bytes() if path.exists() else None
        new = _json(value)
        if new != old:
            before[path] = old
            changes[path] = new
    if not changes:
        return
    check()
    recovery = {str(p): base64.b64encode(data).decode('ascii') if data is not None else None
                for p, data in before.items()}
    _atomic_bytes(_backup_path(backup_name), _json(recovery))
    written = []
    try:
        for path, value in changes.items():
            check()
            _atomic_bytes(path, value)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            if before[path] is None:
                _safe_file(path)
                path.unlink()
            else:
                _atomic_bytes(path, before[path])
        raise


def _settings_plan(account, entry, profile):
    archive, prefs, marker = profile
    current = _read_json(prefs, optional=True)
    incoming = entry['settings']
    for key, val in incoming.items():
        if key == 'ghostModeSettings':
            ghosts = current.setdefault(key, {})
            if not isinstance(ghosts, dict):
                raise ValueError('Invalid destination Ghost Mode preferences.')
            if '0' in val:
                existing = ghosts.setdefault('0', {})
                if not isinstance(existing, dict):
                    raise ValueError('Invalid destination global Ghost Mode preferences.')
                existing.update(val['0'])
        else:
            current[key] = val
    applied = _read_json(marker, optional=True)
    applied['fingerprint'] = hashlib.sha256(json.dumps(ayugram.COMMON_PROFILE, sort_keys=True).encode()).hexdigest()
    applied['inline_history'] = 1
    return {prefs: current, marker: applied}


def _organization(package, mapped):
    if not mapped:
        return
    # Keep the same ordering as other metadata/workspace mutation handlers.
    with state._meta_lock, state._ws_lock:
        meta = copy.deepcopy(state.metadata)
        workspaces = state.load_workspaces()
        by_id = {entry['id']: entry for entry in package['accounts']}
        for identity, path in mapped.items():
            incoming = by_id[identity]['metadata']
            for key in _META_STRINGS:
                if incoming.get(key) and not meta.setdefault(key, {}).get(path):
                    meta[key][path] = incoming[key]
            for key in _PINS:
                pins = meta.setdefault(key, [])
                if incoming.get(key) and path not in pins:
                    pins.append(path)
        for source in package['workspaces']:
            members = [mapped[identity] for identity in source['accounts'] if identity in mapped]
            if not members:
                continue
            target = workspaces.setdefault(source['name'], dict(accounts=[], icon=source['icon'],
                                                                  created=package['created']))
            for path in members:
                if path not in target['accounts']:
                    target['accounts'].append(path)
        _write_files({Path(state.METADATA_FILE): meta, Path(state.WORKSPACES_FILE): workspaces},
                     'organization.json', _require_unlocked)
        state.metadata.clear()
        state.metadata.update(meta)


def _require_unlocked():
    if not remote_history._unlocked():
        raise ValueError('Unlock Manager before transferring data.')
    return True


def _import(payload):
    package = _package(payload.get('package'))
    parts = _include(payload.get('include'))
    if any(parts[key] and not package['include'][key] for key in _PARTS):
        raise ValueError('The package does not include all selected data types.')
    mappings = payload.get('mappings')
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= MAX_ACCOUNTS:
        raise ValueError('Select source accounts and their destination profiles.')
    entries = {entry['id']: entry for entry in package['accounts']}
    selected = {}
    for mapping in mappings:
        _object(mapping, {'id', 'path', 'user_id', 'confirmed'}, 'destination mapping')
        identity = _uuid(mapping['id'])
        if identity not in entries or identity in selected:
            raise ValueError('Invalid or duplicate source account mapping.')
        path = _account(mapping['path'])
        if path in selected.values():
            raise ValueError('Each source account needs a separate destination profile.')
        selected[identity] = path
        history = entries[identity]['history']
        if parts['history'] and history is not None:
            if mapping['confirmed'] is not True or mapping['user_id'] != history['user_id']:
                raise ValueError('Verify and confirm the same numeric Telegram account ID in the destination login.')
    results = []
    completed = {}
    applied_profiles = set()
    with ExitStack() as stack:
        _locks(stack, selected.values())
        plans = {}
        # Validate every destination before applying any selected account.
        for identity, account in selected.items():
            entry = entries[identity]
            profile = None
            files = {}
            if ((parts['history'] and entry['history'] is not None)
                    or (parts['settings'] and entry['settings'] is not None)):
                profile = _profile(account)
                _closed(account, profile[0])
                if parts['history'] and entry['history'] is not None:
                    owners = history_archive.catalog(profile[0])['accounts']
                    if owners and entry['history']['user_id'] not in {v['user_id'] for v in owners}:
                        raise ValueError('Destination history belongs to a different account ID. Nothing was imported.')
                if parts['settings'] and entry['settings'] is not None:
                    files = _settings_plan(account, entry, profile)
            plans[identity] = (profile, files)
        for identity, account in selected.items():
            entry = entries[identity]
            profile, files = plans[identity]
            result = dict(id=identity, path=account, success=False)
            try:
                _require_unlocked()
                _account(account)
                if parts['history'] and entry['history'] is not None:
                    # Destination hash prevents recovery backups for two target
                    # profiles from overwriting each other.
                    target = hashlib.sha256(account.encode()).hexdigest()
                    result['history'] = history_archive.merge_archive(profile[0], entry['history'],
                        source_id=identity, backup_path=_backup_path(target + '.db'),
                        check_closed=lambda: _closed(account, profile[0]), allow_empty=True)
                    applied_profiles.add(identity)
                if files:
                    _write_files(files, hashlib.sha256(account.encode()).hexdigest() + '-settings.json',
                                 lambda: _closed(account, profile[0]))
                    applied_profiles.add(identity)
                completed[identity] = account
                result.update(success=True, message='Selected data imported. Existing sessions were preserved.')
            except _ERRORS as error:
                result['message'] = _message(error)
                if 'history' in result:
                    result['message'] += ' History was already merged; retry to finish the remaining data.'
            results.append(result)
        organization_error = ''
        if parts['organization']:
            try:
                _organization(package, completed)
            except _ERRORS as error:
                organization_error = _message(error)
                for result in results:
                    if result['id'] in completed:
                        result.update(success=False, message='Organization was not imported. ' + organization_error)
    import server
    server.invalidate_scan_cache()
    success = all(result['success'] for result in results) and not organization_error
    partial = not success and bool(applied_profiles or (completed and not organization_error))
    return dict(success=success, partial=partial, results=results,
                message=('Transfer complete. Open your independent logins normally when ready.' if success else
                         organization_error or 'Some data was not imported. Review each result; retrying will not duplicate history.'))


def _message(error):
    if isinstance(error, ValueError) and not isinstance(error, (json.JSONDecodeError, UnicodeError)):
        return str(error)
    return 'Transfer stopped safely. Check file access and available disk space; existing sessions were preserved.'


def request(action, payload):
    try:
        if not isinstance(payload, dict):
            raise ValueError('Invalid transfer request.')
        _require_unlocked()
        if not _transfer_lock.acquire(blocking=False):
            raise ValueError('Another Mac transfer is in progress. Try again shortly.')
        try:
            if action == 'preview':
                return _preview(_package(payload.get('package')))
            if action == 'export':
                return _export(payload)
            if action == 'import':
                return _import(payload)
            if action == 'catalog':
                account = _account(payload.get('path'))
                with ExitStack() as stack:
                    _locks(stack, [account])
                    return _catalog(account)
            raise ValueError('Unsupported transfer action.')
        finally:
            _transfer_lock.release()
    except (RecursionError, TypeError):
        return dict(success=False, message='Invalid or excessively nested transfer data.')
    except _ERRORS as error:
        return dict(success=False, message=_message(error))
