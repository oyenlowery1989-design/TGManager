"""Bounded text-only AyuGram v7.2.9 archive exchange; never reads login files.

Callers bind an explicitly confirmed numeric owner and hold their account lock.
Snapshots and receipts contain private message data and must not be logged.
"""
import base64
from collections import defaultdict
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import tempfile
import uuid

MAX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 100_000
PROTOCOL = 1
TABLES = ('DeletedMessage', 'EditedMessage')
_ID_FIELDS = {'fakeId', 'userId', 'dialogId', 'groupedId', 'peerId', 'fromId',
              'topicId', 'fwdFromId', 'replyPeerId'}
_TEXT_FIELDS = {'fwdName', 'fwdPostAuthor', 'text', 'mediaPath', 'hqThumbPath', 'mimeType'}
_BLOB_FIELDS = {'replySerialized', 'textEntities', 'documentSerialized',
                'thumbsSerialized', 'documentAttributesSerialized'}
COLUMNS = {name: ('TEXT' if name in _TEXT_FIELDS else 'BLOB' if name in _BLOB_FIELDS else 'INTEGER')
           for name in ('fakeId userId dialogId groupedId peerId fromId topicId messageId date flags editDate views '
                        'fwdFlags fwdFromId fwdName fwdDate fwdPostAuthor replyFlags replyMessageId replyPeerId '
                        'replyTopId replyForumTopic replySerialized entityCreateDate text textEntities mediaPath '
                        'hqThumbPath documentType documentSerialized thumbsSerialized documentAttributesSerialized mimeType').split()}
_RECEIPT = 'ManagerHistoryImportReceipt'
_SNAPSHOT = 'ManagerHistoryImportSnapshot'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _number(value, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r'-?(0|[1-9][0-9]{0,18})', value):
        raise ValueError('Invalid numeric archive account or identifier')
    number = int(value)
    if str(number) != value or not -(2**63) <= number < 2**63 or (positive and number <= 0):
        raise ValueError('Invalid numeric archive account or identifier')
    return number


def _safe_file(path, exists=True):
    path = os.path.abspath(os.fspath(path))
    for item in (Path(path), *Path(path).parents):
        if os.path.lexists(item):
            mode = os.lstat(item)
            if stat.S_ISLNK(mode.st_mode):
                raise ValueError('Archive path contains a symbolic link')
    if not os.path.lexists(path):
        if exists:
            raise ValueError('Archive does not exist')
        return None
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Archive must be a regular file without hard links')
    return info.st_dev, info.st_ino


def _check_files(path):
    identity = _safe_file(path)
    if os.stat(path).st_size > MAX_BYTES:
        raise ValueError('Archive exceeds the size limit')
    sidecar_size = 0
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = os.fspath(path) + suffix
        if _safe_file(sidecar, exists=False) is not None:
            sidecar_size += os.stat(sidecar).st_size
    # SQLite journals contain duplicate pages, not additional history. Bound
    # them separately so a valid near-limit transaction can checkpoint safely.
    if sidecar_size > 2 * MAX_BYTES:
        raise ValueError('Archive SQLite sidecars exceed the size limit')
    return identity


def _open(path, write=False):
    identity = _check_files(path)
    connection = sqlite3.connect(Path(path).absolute().as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                 uri=True, timeout=5, isolation_level=None)
    try:
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA foreign_keys=ON')
        if _check_files(path) != identity:
            raise ValueError('Archive changed during opening')
        _schema(connection)
        return connection, identity
    except BaseException:
        connection.close()
        raise


def _schema(db):
    objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if any(kind in ('trigger', 'view') or 'VIRTUAL TABLE' in (sql or '').upper()
           for kind, _, sql in objects):
        raise ValueError('Archive contains unsupported triggers or views')
    for table in ('SchemaVersion', *TABLES):
        expected = COLUMNS if table in TABLES else {'id': 'INTEGER', 'version': 'INTEGER'}
        columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if ({row[1]: row[2].upper() for row in columns} != expected
                or any(row[3] != 1 for row in columns)
                or any(row[5] != (1 if row[1] == ('id' if table == 'SchemaVersion' else 'fakeId') else 0)
                       for row in columns)
                or db.execute(f'PRAGMA foreign_key_list("{table}")').fetchone()):
            raise ValueError('Unsupported AyuGram archive schema')
    if db.execute('SELECT id,version FROM SchemaVersion').fetchall() != [(1, 1)]:
        raise ValueError('Unsupported AyuGram archive schema version')
    definitions = {name: sql for kind, name, sql in objects if kind == 'table'}
    protected = {'SchemaVersion', *TABLES, _RECEIPT, _SNAPSHOT}
    protected_names = {name.casefold(): name for name in protected}
    if any(name.casefold() in protected_names and name != protected_names[name.casefold()]
           for _, name, _ in objects):
        raise ValueError('Archive has a differently cased reserved table name')
    for table in definitions:
        quoted = '"' + table.replace('"', '""') + '"'
        if any(row[2].casefold() in protected_names
               for row in db.execute(f'PRAGMA foreign_key_list({quoted})')):
            raise ValueError('Archive has foreign keys referencing protected history tables')
        if table in protected and any(row[2] and row[3] != 'pk'
                for row in db.execute(f'PRAGMA index_list({quoted})')):
            raise ValueError('Archive has unsupported unique constraints')
    if any('AUTOINCREMENT' not in definitions.get(table, '').upper() for table in TABLES):
        raise ValueError('Unsupported archive revision ID schema')
    for table, names, primary in (
            (_RECEIPT, ('source_id', 'user_id', 'kind', 'identity', 'seen'), 4),
            (_SNAPSHOT, ('source_id', 'user_id', 'digest'), 2)):
        if table in definitions:
            columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            expected = [(name, 'INTEGER' if name == 'seen' else 'TEXT', 1, i + 1 if i < primary else 0)
                        for i, name in enumerate(names)]
            if [(r[1], r[2].upper(), r[3], r[5]) for r in columns] != expected:
                raise ValueError('Unsupported archive import receipt schema')


def _entities(blob, text, owner=None):
    """Validate TL and return semantic tokens; keep native bytes for storage.

    InputUser access hashes and source-message peers are lookup metadata that
    differ between independently authenticated clients. They are not revisions.
    """
    if not blob:
        return ()
    cursor = 0
    semantics = []
    units = len(text.encode('utf-16-le')) // 2

    def take(size):
        nonlocal cursor
        if size < 0 or cursor + size > len(blob):
            raise ValueError('Malformed text entities')
        result = blob[cursor:cursor + size]
        cursor += size
        return result

    def word():
        return struct.unpack('<I', take(4))[0]

    def tl_string():
        first = take(1)[0]
        if first == 255:
            raise ValueError('Malformed entity string')
        length, prefix = (int.from_bytes(take(3), 'little'), 4) if first == 254 else (first, 1)
        raw = take(length)
        try:
            raw.decode('utf-8')
        except UnicodeError:
            raise ValueError('Malformed entity string') from None
        if any(take((-(length + prefix)) % 4)):
            raise ValueError('Malformed entity padding')

    def peer(depth=0):
        if depth > 8:
            raise ValueError('Entity nesting limit exceeded')
        kind = word()
        if kind in (0x7f3b18ea, 0x7da07ec9):
            return
        if kind == 0x35a95cb9:
            take(8)
        elif kind in (0xdde8a54c, 0x27bcbbfc):
            take(16)
        elif kind in (0xa87b0a1c, 0xbd2a0840):
            peer(depth + 1)
            take(12)
        else:
            raise ValueError('Unsupported entity peer')

    simple = {0xbb92ba95, 0xfa04579d, 0x6f635b0d, 0x6cef8ac7, 0x6ed02538,
              0x64e475c2, 0xbd610bc9, 0x826f8b60, 0x28a20571, 0x9b69e34b,
              0x4c4e743f, 0x9c4e7e8b, 0xbf0693d4, 0x761e6af4, 0x32ca960f,
              0x71777116, 0x0652c1c5}
    if len(blob) % 4 or word() != 0x1cb5c415:
        raise ValueError('Malformed text entity vector')
    count = word()
    if count > min(100_000, len(blob) // 12):
        raise ValueError('Text entity count exceeds its bounds')
    for _ in range(count):
        start = cursor
        kind = word()
        if kind in (0xf1ccaaac, 0x904ac7c7):
            flags = word()
            if flags & ~(1 if kind == 0xf1ccaaac else 63):
                raise ValueError('Unsupported entity flags')
        offset, length = struct.unpack('<ii', take(8))
        if offset < 0 or length < 0 or offset + length > units:
            raise ValueError('Text entity offsets exceed text')
        if kind in simple or kind == 0xf1ccaaac:
            semantics.append(blob[start:cursor].hex())
            continue
        if kind in (0x73924be0, 0x76a6d327, 0xc6c1e5a7):
            tl_string()
        elif kind == 0xdc7b1140:
            target = struct.unpack('<q', take(8))[0]
            semantics.append(('mention', offset, length, target))
            continue
        elif kind == 0xc8cf05f8:
            take(8)
        elif kind == 0x904ac7c7:
            take(4)
        elif kind == 0x208e68c9:
            user_kind = word()
            if user_kind == 0xb98886cf:
                target = 'empty'
            elif user_kind == 0xf7c1b13f:
                target = owner if owner is not None else 'self'
            elif user_kind == 0xf21158c6:
                target = struct.unpack('<q', take(8))[0]
                take(8)  # Access hash is session-specific lookup metadata.
            elif user_kind == 0x1da448e2:
                peer()
                take(4)  # Source message context, not the mentioned user.
                target = struct.unpack('<q', take(8))[0]
            else:
                raise ValueError('Unsupported mention entity')
            semantics.append(('mention', offset, length, target))
            continue
        else:
            raise ValueError('Unsupported text entity constructor')
        semantics.append(blob[start:cursor].hex())
    if cursor != len(blob):
        raise ValueError('Trailing text entity data')
    return tuple(semantics)


def _row(row, wire=False):
    if not isinstance(row, dict) or set(row) != set(COLUMNS):
        raise ValueError('Unsupported archive row fields')
    result = {}
    for name, kind in COLUMNS.items():
        value = row[name]
        if kind == 'INTEGER':
            if wire and name in _ID_FIELDS:
                value = _number(value)
            limit = 2**63 if name in _ID_FIELDS else 2**31
            if type(value) is not int or not -limit <= value < limit:
                raise ValueError('Invalid archive integer')
        elif kind == 'TEXT':
            if not isinstance(value, str):
                raise ValueError('Invalid archive text')
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise ValueError('Invalid archive text encoding') from None
        else:
            if wire:
                if not isinstance(value, str):
                    raise ValueError('Invalid archive binary value')
                try:
                    decoded = base64.b64decode(value, validate=True)
                except (ValueError, base64.binascii.Error):
                    raise ValueError('Invalid archive binary encoding') from None
                if base64.b64encode(decoded).decode('ascii') != value:
                    raise ValueError('Noncanonical archive binary encoding')
                value = decoded
            if type(value) is not bytes:
                raise ValueError('Invalid archive binary value')
        result[name] = value
    if (result['fakeId'] <= 0 or result['userId'] <= 0 or result['messageId'] <= 0
            or result['date'] <= 0 or result['replyForumTopic'] not in (0, 1) or not result['text']):
        raise ValueError('Invalid archive message identity')
    if (result['mediaPath'] not in ('', '/') or result['hqThumbPath'] or result['documentType'] != 0
            or result['mimeType'] or any(result[x] for x in _BLOB_FIELDS - {'textEntities'})):
        raise ValueError('Unsupported archive media or reply payload')
    _entities(result['textEntities'], result['text'])
    return result


def _wire(row):
    return {name: (str(value) if name in _ID_FIELDS else base64.b64encode(value).decode('ascii')
                   if name in _BLOB_FIELDS else value) for name, value in row.items()}


def _rows(db, table, user_id):
    cursor = db.cursor()
    cursor.row_factory = sqlite3.Row
    rows = cursor.execute(f'SELECT * FROM "{table}" WHERE userId=? ORDER BY fakeId LIMIT ?',
                      (user_id, MAX_RECORDS + 1)).fetchall()
    if len(rows) > MAX_RECORDS:
        raise ValueError('Archive exceeds the record limit')
    return [_row(dict(row)) for row in rows]


def catalog(path):
    db, identity = _open(path)
    try:
        db.execute('BEGIN')
        _schema(db)
        if _check_files(path) != identity:
            raise ValueError('Archive changed during snapshot opening')
        owners = defaultdict(lambda: {'deleted': 0, 'edited': 0})
        total = 0
        for table, kind in zip(TABLES, ('deleted', 'edited')):
            for owner, count in db.execute(f'SELECT userId,COUNT(*) FROM "{table}" GROUP BY userId'):
                _number(str(owner), positive=True)
                total += count
                if total > MAX_RECORDS:
                    raise ValueError('Archive exceeds the record limit')
                owners[owner][kind] = count
        if _check_files(path) != identity:
            raise ValueError('Archive changed during catalog')
        return {'protocol': PROTOCOL, 'schema_version': 1, 'accounts':
                [dict(user_id=str(owner), **owners[owner]) for owner in sorted(owners)]}
    finally:
        db.close()


def _body(user_id, deleted, edited):
    return dict(protocol=PROTOCOL, schema_version=1, user_id=user_id, deleted=deleted, edited=edited)


def export_archive(path, user_id, known_digest=''):
    owner = _number(user_id, positive=True)
    if known_digest and not re.fullmatch('[0-9a-f]{64}', known_digest):
        raise ValueError('Invalid snapshot digest')
    db, identity = _open(path)
    try:
        db.execute('BEGIN')
        _schema(db)
        if _check_files(path) != identity:
            raise ValueError('Archive changed during snapshot opening')
        rows = [_rows(db, table, owner) for table in TABLES]
        if not any(rows):
            raise ValueError('Account ID is absent from archive')
        if sum(map(len, rows)) > MAX_RECORDS:
            raise ValueError('Archive exceeds the record limit')
        body = _body(user_id, *[[ _wire(row) for row in group] for group in rows])
        encoded = _json(body)
        if len(encoded) > MAX_BYTES - 256:
            raise ValueError('Archive exceeds the transfer size limit')
        digest = hashlib.sha256(encoded).hexdigest()
        if _check_files(path) != identity:
            raise ValueError('Archive changed during export')
        if digest == known_digest:
            body = _body(user_id, [], [])
        return dict(body, digest=digest, unchanged=digest == known_digest)
    finally:
        db.close()


def _envelope(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {'protocol', 'schema_version', 'user_id', 'deleted', 'edited', 'digest', 'unchanged'}:
        raise ValueError('Invalid archive snapshot envelope')
    if len(_json(envelope)) > MAX_BYTES:
        raise ValueError('Archive exceeds the transfer size limit')
    if (type(envelope['protocol']) is not int or envelope['protocol'] != PROTOCOL
            or type(envelope['schema_version']) is not int or envelope['schema_version'] != 1
            or type(envelope['unchanged']) is not bool):
        raise ValueError('A complete compatible archive snapshot is required')
    owner = _number(envelope['user_id'], positive=True)
    if not isinstance(envelope['digest'], str) or not re.fullmatch('[0-9a-f]{64}', envelope['digest']):
        raise ValueError('Invalid archive snapshot digest')
    if any(not isinstance(envelope[kind], list) for kind in ('deleted', 'edited')):
        raise ValueError('Invalid archive snapshot rows')
    if sum(len(envelope[kind]) for kind in ('deleted', 'edited')) > MAX_RECORDS:
        raise ValueError('Archive exceeds the record limit')
    if envelope['unchanged']:
        if envelope['deleted'] or envelope['edited']:
            raise ValueError('Unchanged snapshot must omit message rows')
        return owner, [[], []]
    body = _body(envelope['user_id'], envelope['deleted'], envelope['edited'])
    if hashlib.sha256(_json(body)).hexdigest() != envelope['digest']:
        raise ValueError('Archive snapshot digest does not match')
    rows = [[_row(row, wire=True) for row in envelope[kind]] for kind in ('deleted', 'edited')]
    for group in rows:
        ids = [row['fakeId'] for row in group]
        if ids != sorted(set(ids)) or any(row['userId'] != owner for row in group):
            raise ValueError('Archive snapshot owner or row ordering does not match')
    if not any(rows):
        raise ValueError('Account ID is absent from snapshot')
    return owner, rows


def _backup(path, backup_path):
    backup_path = os.path.abspath(os.fspath(backup_path))
    if backup_path == os.path.abspath(os.fspath(path)):
        raise ValueError('Archive backup must be a separate file')
    _safe_file(backup_path, exists=False)
    for suffix in ('-wal', '-shm', '-journal'):
        if os.path.lexists(backup_path + suffix):
            _safe_file(backup_path + suffix)
            raise ValueError('Archive backup has unexpected SQLite sidecars')
    parent = os.path.dirname(backup_path)
    if not os.path.exists(parent):
        grandparent = os.path.dirname(parent)
        _safe_file(os.path.join(grandparent, '.unused'), exists=False)
        os.mkdir(parent, 0o700)
    if os.stat(parent).st_mode & 0o077:
        raise ValueError('Archive backup directory must be private')
    fd, temporary = tempfile.mkstemp(prefix='.history-', suffix='.db', dir=parent)
    os.close(fd)
    source = target = None
    try:
        source, _ = _open(path)
        target = sqlite3.connect(temporary)
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Archive backup integrity check failed')
        target.close()
        target = None
        with open(temporary, 'rb') as saved:
            os.fsync(saved.fileno())
        _safe_file(backup_path, exists=False)
        os.replace(temporary, backup_path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if source:
            source.close()
        if target:
            target.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def _message_key(row):
    return row['userId'], row['dialogId'], row['messageId']


def _edit_key(row):
    edited = bool(row['flags'] & 0x8000)
    return (*_message_key(row), edited, row['editDate'] if edited else row['date'], row['text'],
            _entities(row['textEntities'], row['text'], row['userId']))


def _token(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _insert(db, table, row):
    names = [name for name in COLUMNS if name != 'fakeId']
    db.execute(f'INSERT INTO "{table}" ({",".join(names)}) VALUES ({",".join("?" for _ in names)})',
               [row[name] for name in names])


def _closed(check_closed):
    if check_closed() is not True:
        raise ValueError('Local AyuGram must be fully closed')


def _binding(source_id):
    try:
        if str(uuid.UUID(source_id)) != source_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Invalid history source binding ID') from None


def imported_digest(path, source_id, user_id):
    """Negotiate against the archive's own receipt, so restored DBs catch up."""
    _binding(source_id)
    _number(user_id, positive=True)
    db, identity = _open(path)
    try:
        db.execute('BEGIN')
        _schema(db)
        if _check_files(path) != identity:
            raise ValueError('Archive changed during snapshot opening')
        row = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_SNAPSHOT,)).fetchone():
            row = db.execute(f'SELECT digest FROM {_SNAPSHOT} WHERE source_id=? AND user_id=?',
                             (source_id, user_id)).fetchone()
        digest = row[0] if row else ''
        if not isinstance(digest, str) or (digest and not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Invalid archive import receipt')
        if _check_files(path) != identity:
            raise ValueError('Archive changed during receipt read')
        return digest
    finally:
        db.close()


def _ordered_edits(old, added, remote):
    """Merge two partial capture orders; preserve local order on conflicts."""
    def tokens(rows):
        occurrences = defaultdict(int)
        result = []
        for row in rows:
            semantic = _edit_key(row)
            occurrences[semantic] += 1
            result.append(((semantic, occurrences[semantic]), row))
        return result

    local_tokens = tokens(old)
    remote_tokens = tokens(remote)
    additions = {row['fakeId'] for row in added}
    nodes = dict(local_tokens)
    nodes.update((token, row) for token, row in remote_tokens if row['fakeId'] in additions)
    buckets = defaultdict(set)
    for token in nodes:
        buckets[token[0][3:5]].add(token)
    left_buckets = defaultdict(list)
    right_buckets = defaultdict(list)
    for token, _ in local_tokens:
        left_buckets[token[0][3:5]].append(token)
    for token, _ in remote_tokens:
        if token in nodes:
            right_buckets[token[0][3:5]].append(token)
    merged = []
    ambiguities = 0
    for stamp in sorted(buckets):
        members = buckets[stamp]
        left = left_buckets[stamp]
        right = right_buckets[stamp]
        priority = {token: (0, i) for i, token in enumerate(left)}
        priority.update((token, (1, i)) for i, token in enumerate(right) if token not in priority)
        outgoing = {token: set() for token in members}
        degree = dict.fromkeys(members, 0)
        for sequence in (left, right):
            for first, second in zip(sequence, sequence[1:]):
                if second not in outgoing[first]:
                    outgoing[first].add(second)
                    degree[second] += 1
        ready = [(priority[token], token) for token in members if not degree[token]]
        heapq.heapify(ready)
        ordered = []
        ambiguous = False
        while ready:
            ambiguous |= len(ready) > 1
            _, token = heapq.heappop(ready)
            ordered.append(token)
            for next_token in outgoing[token]:
                degree[next_token] -= 1
                if not degree[next_token]:
                    heapq.heappush(ready, (priority[next_token], next_token))
        if len(ordered) != len(members):
            local_set = set(left)
            ordered = left + [token for token in right if token not in local_set]
            ambiguous = True
        ambiguities += int(ambiguous)
        merged.extend(nodes[token] for token in ordered)
    return merged, ambiguities


def merge_archive(path, envelope, source_id, backup_path, check_closed, *, allow_empty=False):
    owner, incoming = _envelope(envelope)
    _binding(source_id)
    _closed(check_closed)
    db, identity = _open(path, write=True)
    result = dict(digest=envelope['digest'], user_id=str(owner), imported=dict(deleted=0, edited=0),
                  skipped=dict(deleted=0, edited=0), conflicts=0, ambiguities=0, unchanged=False)
    try:
        db.execute('BEGIN IMMEDIATE')
        _schema(db)
        local = [_rows(db, table, owner) for table in TABLES]
        if sum(map(len, local)) > MAX_RECORDS:
            raise ValueError('Archive exceeds the record limit')
        previous = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (_SNAPSHOT,)).fetchone():
            previous = db.execute(f'SELECT digest FROM {_SNAPSHOT} WHERE source_id=? AND user_id=?',
                                  (source_id, str(owner))).fetchone()
            if previous and previous[0] == envelope['digest']:
                result['unchanged'] = True
                result['skipped'] = dict(deleted=len(incoming[0]), edited=len(incoming[1]))
                return result
        if envelope['unchanged']:
            raise ValueError('Unchanged snapshot does not match a local import receipt')
        # A new Mac may have an initialized but empty archive. Only the selective
        # transfer flow, after explicit numeric account confirmation, opts in.
        # Remote sync retains its existing requirement for local owner evidence.
        if not any(local) and previous is None:
            empty = not any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in TABLES)
            if allow_empty is not True or not empty:
                raise ValueError('Account ID is absent from local archive')
        _backup(path, backup_path)
        db.execute(f'CREATE TABLE IF NOT EXISTS {_RECEIPT}(source_id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL, seen INTEGER NOT NULL, PRIMARY KEY(source_id,user_id,kind,identity))')
        db.execute(f'CREATE TABLE IF NOT EXISTS {_SNAPSHOT}(source_id TEXT NOT NULL,user_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(source_id,user_id))')
        receipts = {(r[0], r[1]): r[2] for r in db.execute(
            f'SELECT kind,identity,seen FROM {_RECEIPT} WHERE source_id=? AND user_id=?', (source_id, str(owner)))}

        if any(kind not in ('deleted', 'edited') or not isinstance(token, str)
               or not re.fullmatch('[0-9a-f]{64}', token) or type(count) is not int
               or not 1 <= count <= MAX_RECORDS for (kind, token), count in receipts.items()):
            raise ValueError('Invalid archive import receipt')

        def seen(kind, token, count):
            db.execute(f'INSERT INTO {_RECEIPT} VALUES(?,?,?,?,?) ON CONFLICT(source_id,user_id,kind,identity) DO UPDATE SET seen=MAX(seen,excluded.seen)',
                       (source_id, str(owner), kind, token, count))

        deleted = defaultdict(list)
        for row in local[0]:
            deleted[_message_key(row)].append(row)
        for row in incoming[0]:
            key = _message_key(row)
            token = _token(key)
            # Capture timestamps, view counts and mutable status flags differ on
            # independent clients without changing the archived message content.
            content_fields = ('text', 'fromId', 'topicId', 'date',
                              'fwdFromId', 'fwdName', 'fwdPostAuthor', 'replyMessageId', 'replyPeerId')
            if key in deleted and any(any(old[k] != row[k] for k in content_fields)
                                      or _entities(old['textEntities'], old['text'], old['userId'])
                                      != _entities(row['textEntities'], row['text'], row['userId'])
                                      for old in deleted[key]):
                result['conflicts'] += 1
            if ('deleted', token) in receipts:
                result['skipped']['deleted'] += 1
            elif key in deleted:
                result['skipped']['deleted'] += 1
            else:
                _insert(db, TABLES[0], row)
                deleted[key].append(row)
                result['imported']['deleted'] += 1
            seen('deleted', token, 1)
            receipts['deleted', token] = 1

        existing = defaultdict(list)
        remote = defaultdict(list)
        for row in local[1]:
            existing[_edit_key(row)].append(row)
        for row in incoming[1]:
            remote[_edit_key(row)].append(row)
        additions = defaultdict(list)
        local_messages = defaultdict(list)
        remote_messages = defaultdict(list)
        for row in local[1]:
            local_messages[_message_key(row)].append(row)
        for row in incoming[1]:
            remote_messages[_message_key(row)].append(row)
        for key, group in remote.items():
            token = _token(key)
            prior = receipts.get(('edited', token), 0)
            needed = max(0, len(group) - max(len(existing[key]), prior))
            additions[_message_key(group[0])].extend(group[-needed:] if needed else [])
            result['imported']['edited'] += needed
            result['skipped']['edited'] += len(group) - needed
            seen('edited', token, len(group))
        for key, added in additions.items():
            merged, ambiguities = _ordered_edits(local_messages[key], added, remote_messages[key])
            result['ambiguities'] += ambiguities
            if not added:
                continue
            sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='EditedMessage'").fetchone()
            maximum = max(sequence[0] if sequence else 0,
                          db.execute('SELECT COALESCE(MAX(fakeId),0) FROM EditedMessage').fetchone()[0])
            if maximum + len(merged) >= 2**63:
                raise ValueError('Archive revision IDs are exhausted')
            db.execute('DELETE FROM EditedMessage WHERE userId=? AND dialogId=? AND messageId=?', key)
            for row in merged:
                _insert(db, TABLES[1], row)
        db.execute(f'INSERT INTO {_SNAPSHOT} VALUES(?,?,?) ON CONFLICT(source_id,user_id) DO UPDATE SET digest=excluded.digest',
                   (source_id, str(owner), envelope['digest']))
        total = sum(db.execute(f'SELECT COUNT(*) FROM {table} WHERE userId=?', (owner,)).fetchone()[0]
                    for table in TABLES)
        if total > MAX_RECORDS:
            raise ValueError('Merged archive exceeds the record limit')
        allocated = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
        if allocated > MAX_BYTES:
            raise ValueError('Merged archive exceeds the size limit')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Archive integrity check failed')
        if _check_files(path) != identity:
            raise ValueError('Archive changed during import')
        _closed(check_closed)
        db.commit()
        return result
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()
