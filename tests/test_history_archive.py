"""Synthetic archives only: no real account files or processes."""
import base64
import copy
from contextlib import contextmanager
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest import mock

RESOURCE = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources/history_archive.py'
spec = importlib.util.spec_from_file_location('history_archive', RESOURCE)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


@contextmanager
def connect(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.make_db('source.db')
        self.dest = self.make_db('dest.db')
        self.insert(self.dest, 'DeletedMessage', self.row('owner marker', message=999))
        self.backup = self.root / 'private' / 'history.db'
        self.binding = 'c836886d-b1b1-4d81-b2c8-cfab19ce42ad'

    def make_db(self, name):
        path = self.root / name
        with connect(path) as db:
            db.execute('CREATE TABLE SchemaVersion(id INTEGER PRIMARY KEY NOT NULL, version INTEGER NOT NULL)')
            db.execute('INSERT INTO SchemaVersion VALUES(1,1)')
            for table in ('DeletedMessage', 'EditedMessage'):
                fields = ['"fakeId" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL']
                fields += [f'"{name}" {kind} NOT NULL' for name, kind in archive.COLUMNS.items() if name != 'fakeId']
                db.execute(f'CREATE TABLE {table}({",".join(fields)})')
            db.execute('CREATE TABLE Unrelated(value TEXT)')
            db.execute("INSERT INTO Unrelated VALUES('untouched')")
        return path

    def row(self, text='hello', message=10, user=1, edited=False, stamp=100):
        row = {name: (b'' if kind == 'BLOB' else '' if kind == 'TEXT' else 0)
               for name, kind in archive.COLUMNS.items() if name != 'fakeId'}
        row.update(userId=user, dialogId=2, peerId=2, fromId=user, messageId=message,
                   date=10, editDate=stamp, entityCreateDate=stamp, flags=0x8000 if edited else 0,
                   text=text, mediaPath='/')
        return row

    def insert(self, path, table, row):
        with connect(path) as db:
            columns = ','.join(f'"{x}"' for x in row)
            return db.execute(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in row)})', tuple(row.values())).lastrowid

    def rows(self, table='EditedMessage'):
        with connect(self.dest) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE messageId != 999 ORDER BY fakeId')]

    def envelope(self):
        return archive.export_archive(self.source, '1')

    def merge(self, envelope=None, check=lambda: True):
        return archive.merge_archive(self.dest, envelope or self.envelope(), self.binding, self.backup, check)

    def test_catalog_and_snapshot_owner_isolation(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        self.insert(self.source, 'EditedMessage', self.row(user=2))
        self.assertEqual(archive.catalog(self.source)['accounts'], [dict(user_id='1', deleted=1, edited=0), dict(user_id='2', deleted=0, edited=1)])
        envelope = self.envelope()
        self.assertEqual(len(envelope['deleted']), 1)
        self.assertEqual(envelope['edited'], [])
        self.assertEqual(envelope['deleted'][0]['userId'], '1')
        self.assertTrue(archive.export_archive(self.source, '1', envelope['digest'])['unchanged'])
        with self.assertRaises(ValueError):
            archive.export_archive(self.source, '3')

    def test_deleted_conflicts_preserve_local_and_replays_stay_deleted(self):
        self.insert(self.dest, 'DeletedMessage', self.row('local'))
        self.insert(self.source, 'DeletedMessage', self.row('remote'))
        result = self.merge()
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(self.rows('DeletedMessage')[0]['text'], 'local')
        with connect(self.dest) as db:
            db.execute('DELETE FROM DeletedMessage')
        self.assertEqual(self.merge()['imported']['deleted'], 0)
        self.assertEqual(self.rows('DeletedMessage'), [])

    def test_repeat_and_source_reset_do_not_duplicate_or_resurrect(self):
        self.insert(self.source, 'EditedMessage', self.row('A'))
        self.assertEqual(self.merge()['imported']['edited'], 1)
        self.assertEqual(self.merge()['imported']['edited'], 0)
        with connect(self.dest) as db:
            db.execute('DELETE FROM EditedMessage')
        self.insert(self.source, 'DeletedMessage', self.row(message=11))
        self.assertEqual(self.merge()['imported']['edited'], 0)
        self.assertEqual(self.rows(), [])

    def test_edit_overlap_preserves_occurrence_counts_and_orders_affected_only(self):
        unrelated = self.insert(self.dest, 'EditedMessage', self.row('other', message=99))
        for text, stamp in [('A', 100), ('B', 200), ('A', 300)]:
            self.insert(self.source, 'EditedMessage', self.row(text, edited=True, stamp=stamp))
        self.insert(self.dest, 'EditedMessage', self.row('B', edited=True, stamp=200))
        result = self.merge()
        self.assertEqual(result['imported']['edited'], 2)
        rows = self.rows()
        self.assertEqual([r['text'] for r in rows if r['messageId'] == 10], ['A', 'B', 'A'])
        self.assertEqual(next(r['fakeId'] for r in rows if r['messageId'] == 99), unrelated)
        with connect(self.dest) as db:
            self.assertEqual(db.execute('SELECT value FROM Unrelated').fetchone()[0], 'untouched')

    def test_duplicate_equal_revision_occurrences_survive(self):
        for _ in range(3):
            self.insert(self.source, 'EditedMessage', self.row('A', edited=True))
        self.insert(self.dest, 'EditedMessage', self.row('A', edited=True))
        self.assertEqual(self.merge()['imported']['edited'], 2)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.merge()['imported']['edited'], 0)

    def test_original_revision_uses_message_date_and_reports_conflicting_ties(self):
        self.insert(self.source, 'EditedMessage', self.row('original', stamp=999))
        self.insert(self.source, 'EditedMessage', self.row('remote', edited=True, stamp=200))
        self.insert(self.dest, 'EditedMessage', self.row('local', edited=True, stamp=200))
        result = self.merge()
        self.assertGreater(result['ambiguities'], 0)
        self.assertEqual([r['text'] for r in self.rows()], ['original', 'local', 'remote'])

    def test_closed_recheck_rolls_back_and_private_backup_has_premerge_data(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        checks = iter((True, False))
        with self.assertRaises(ValueError):
            self.merge(check=lambda: next(checks))
        self.assertEqual(self.rows('DeletedMessage'), [])
        self.assertEqual(os.stat(self.backup).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.backup.parent).st_mode & 0o777, 0o700)
        with connect(self.backup) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM DeletedMessage WHERE messageId != 999').fetchone()[0], 0)
        self.assertEqual(self.merge()['imported']['deleted'], 1)

    def test_rejects_unknown_schema_triggers_and_links(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        with connect(self.source) as db:
            db.execute('ALTER TABLE EditedMessage ADD COLUMN surprise TEXT')
        with self.assertRaises(ValueError):
            self.envelope()
        with connect(self.dest) as db:
            db.execute('CREATE TRIGGER bad AFTER INSERT ON DeletedMessage BEGIN DELETE FROM Unrelated; END')
        with self.assertRaises(ValueError):
            archive.catalog(self.dest)
        linked = self.root / 'linked.db'
        linked.symlink_to(self.dest)
        with self.assertRaises(ValueError):
            archive.catalog(linked)
        hard = self.root / 'hard.db'
        os.link(self.dest, hard)
        with self.assertRaises(ValueError):
            archive.catalog(self.dest)

    def test_rejects_journal_links_and_symlink_parent(self):
        journal = Path(str(self.source) + '-wal')
        journal.symlink_to(self.dest)
        with self.assertRaises(ValueError):
            self.envelope()
        journal.unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            archive.catalog(alias / self.source.name)

    def test_validation_rejects_bad_types_owner_digest_entities_media(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        envelope = self.envelope()
        for field, bad in [('userId', '2'), ('messageId', True), ('mediaPath', '/remote/file'), ('textEntities', 'AA==')]:
            changed = copy.deepcopy(envelope)
            changed['deleted'][0][field] = bad
            body = {k: changed[k] for k in ('protocol', 'schema_version', 'user_id', 'deleted', 'edited')}
            changed['digest'] = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.merge(changed)
        envelope['digest'] = '0' * 64
        with self.assertRaises(ValueError):
            self.merge(envelope)

    def test_valid_entities_round_trip_and_malformed_offsets_fail(self):
        row = self.row('hello')
        row['textEntities'] = struct.pack('<IIIII', 0x1cb5c415, 1, 0xbd610bc9, 0, 5)
        self.insert(self.source, 'DeletedMessage', row)
        self.merge()
        self.assertEqual(self.rows('DeletedMessage')[0]['textEntities'], row['textEntities'])
        row['textEntities'] = struct.pack('<IIIII', 0x1cb5c415, 1, 0xbd610bc9, 0, 99)
        self.insert(self.source, 'DeletedMessage', row)
        with self.assertRaises(ValueError):
            self.envelope()

    def test_digest_receipts_are_read_from_database_and_validate_unchanged(self):
        self.insert(self.source, 'EditedMessage', self.row())
        self.assertEqual(archive.imported_digest(self.dest, self.binding, '1'), '')
        envelope = self.envelope()
        self.merge(envelope)
        self.assertEqual(archive.imported_digest(self.dest, self.binding, '1'), envelope['digest'])
        unchanged = archive.export_archive(self.source, '1', envelope['digest'])
        self.assertTrue(self.merge(unchanged)['unchanged'])
        unchanged['digest'] = '0' * 64
        with self.assertRaises(ValueError):
            self.merge(unchanged)

    def test_same_timestamp_compatible_order_inserts_between_common_anchors(self):
        for text in ('A', 'C'):
            self.insert(self.dest, 'EditedMessage', self.row(text, edited=True))
        for text in ('A', 'B', 'C'):
            self.insert(self.source, 'EditedMessage', self.row(text, edited=True))
        result = self.merge()
        self.assertEqual([r['text'] for r in self.rows()], ['A', 'B', 'C'])
        self.assertEqual(result['ambiguities'], 0)

    def test_same_timestamp_conflicting_order_keeps_local_order(self):
        for text in ('A', 'C'):
            self.insert(self.dest, 'EditedMessage', self.row(text, edited=True))
        for text in ('C', 'B', 'A'):
            self.insert(self.source, 'EditedMessage', self.row(text, edited=True))
        result = self.merge()
        self.assertEqual([r['text'] for r in self.rows()], ['A', 'C', 'B'])
        self.assertGreater(result['ambiguities'], 0)

    def test_source_reset_and_capture_metadata_changes_do_not_duplicate_edits(self):
        self.insert(self.source, 'EditedMessage', self.row('A', edited=True))
        self.merge()
        with connect(self.source) as db:
            db.execute('DELETE FROM EditedMessage')
        changed = self.row('A', edited=True)
        changed['entityCreateDate'] = 500
        changed['views'] = 12
        self.insert(self.source, 'EditedMessage', changed)
        self.assertEqual(self.merge()['imported']['edited'], 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['views'], 0)

    def test_missing_local_owner_fails_without_backup_or_mutation(self):
        self.insert(self.source, 'DeletedMessage', self.row(user=2))
        envelope = archive.export_archive(self.source, '2')
        with self.assertRaises(ValueError):
            self.merge(envelope)
        self.assertFalse(self.backup.exists())

    def test_previous_backup_survives_backup_publication_failure(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        self.merge()
        previous = self.backup.read_bytes()
        self.insert(self.source, 'DeletedMessage', self.row(message=11))
        with mock.patch.object(archive.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.merge()
        self.assertEqual(self.backup.read_bytes(), previous)
        self.assertEqual(len(self.rows('DeletedMessage')), 1)
        self.assertFalse(list(self.backup.parent.glob('.history-*')))

    def test_restored_database_receipt_requests_full_snapshot_again(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        self.merge()
        self.assertNotEqual(archive.imported_digest(self.dest, self.binding, '1'), '')
        self.assertEqual(archive.imported_digest(self.backup, self.binding, '1'), '')

    def test_binary_entity_validation_rejects_trailing_unknown_and_oversize(self):
        for entities in (struct.pack('<II', 0x1cb5c415, 99),
                         struct.pack('<IIIII', 0x1cb5c415, 1, 0x12345678, 0, 1),
                         struct.pack('<III', 0x1cb5c415, 0, 0)):
            with self.subTest(entities=entities), self.assertRaises(ValueError):
                archive._entities(entities, 'hello')

    def test_conflicting_same_timestamp_order_is_reported_without_additions(self):
        for text in ('A', 'C'):
            self.insert(self.dest, 'EditedMessage', self.row(text, edited=True))
        for text in ('C', 'A'):
            self.insert(self.source, 'EditedMessage', self.row(text, edited=True))
        before = self.rows()
        result = self.merge()
        self.assertEqual(result['imported']['edited'], 0)
        self.assertGreater(result['ambiguities'], 0)
        self.assertEqual(self.rows(), before)

    def test_conflicting_source_deleted_duplicates_are_reported(self):
        self.insert(self.source, 'DeletedMessage', self.row('first'))
        self.insert(self.source, 'DeletedMessage', self.row('second'))
        result = self.merge()
        self.assertEqual(result['imported']['deleted'], 1)
        self.assertEqual(result['conflicts'], 1)
        self.assertEqual(self.rows('DeletedMessage')[0]['text'], 'first')

    def test_merged_owner_record_limit_rolls_back(self):
        self.insert(self.source, 'DeletedMessage', self.row(message=11))
        self.insert(self.source, 'DeletedMessage', self.row(message=12))
        envelope = self.envelope()
        with mock.patch.object(archive, 'MAX_RECORDS', 2), self.assertRaises(ValueError):
            self.merge(envelope)
        self.assertEqual(self.rows('DeletedMessage'), [])

    def test_sidecar_size_limit_precedes_sqlite_reads(self):
        wal = Path(str(self.source) + '-wal')
        with wal.open('wb') as stream:
            stream.truncate(2 * archive.MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            archive.catalog(self.source)

    def test_limits_and_backup_failure_preserve_destination(self):
        self.insert(self.source, 'DeletedMessage', self.row())
        with mock.patch.object(archive, 'MAX_RECORDS', 0), self.assertRaises(ValueError):
            self.envelope()
        with mock.patch.object(archive, 'MAX_BYTES', 10), self.assertRaises(ValueError):
            self.envelope()
        self.backup.parent.mkdir()
        self.backup.symlink_to(self.dest)
        with self.assertRaises(ValueError):
            self.merge()
        self.assertEqual(self.rows('DeletedMessage'), [])

    def test_unsupported_unique_constraint_cannot_replace_local_message(self):
        with connect(self.dest) as db:
            db.execute('ALTER TABLE DeletedMessage RENAME TO OriginalDeleted')
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='OriginalDeleted'").fetchone()[0]
            sql = sql.replace('"OriginalDeleted"', 'DeletedMessage')
            sql = sql[:sql.rfind(')')] + ', UNIQUE(dialogId) ON CONFLICT REPLACE)'
            db.execute(sql)
            db.execute('INSERT INTO DeletedMessage SELECT * FROM OriginalDeleted')
            db.execute('DROP TABLE OriginalDeleted')
        self.insert(self.source, 'DeletedMessage', self.row(message=10))
        with self.assertRaisesRegex(ValueError, 'unique constraints'):
            self.merge()
        with connect(self.dest) as db:
            self.assertEqual(db.execute('SELECT messageId FROM DeletedMessage').fetchall(), [(999,)])

    def test_inbound_foreign_key_cannot_cascade_on_rekeyed_edits(self):
        with connect(self.dest) as db:
            db.execute('CREATE TABLE CustomRef(id INTEGER REFERENCES EditedMessage(fakeId) ON DELETE CASCADE)')
        self.insert(self.source, 'EditedMessage', self.row())
        with self.assertRaisesRegex(ValueError, 'foreign keys'):
            self.merge()
        self.assertFalse(self.backup.exists())

    def test_differently_cased_receipt_table_cannot_bypass_schema_validation(self):
        with connect(self.dest) as db:
            db.execute('CREATE TABLE managerhistoryimportreceipt(unexpected TEXT)')
        self.insert(self.source, 'DeletedMessage', self.row())
        with self.assertRaisesRegex(ValueError, 'reserved table name'):
            self.merge()
        self.assertFalse(self.backup.exists())

    def test_logical_growth_limit_rolls_back_before_success(self):
        self.insert(self.source, 'DeletedMessage', self.row(text='x' * 25000))
        envelope = self.envelope()
        before = self.dest.read_bytes()
        with mock.patch.object(archive, 'MAX_BYTES', 60000), self.assertRaisesRegex(ValueError, 'size limit'):
            archive.merge_archive(self.dest, envelope, self.binding, self.backup, lambda: True)
        self.assertEqual(before, self.dest.read_bytes())
        self.assertEqual(archive.catalog(self.dest)['accounts'][0]['deleted'], 1)

    def test_capture_times_and_views_do_not_create_false_content_conflicts(self):
        self.insert(self.dest, 'DeletedMessage', self.row(text='same text', stamp=123))
        row = self.row(text='same text', stamp=456); row['views'] = 99
        self.insert(self.source, 'DeletedMessage', row)
        result = self.merge()
        self.assertEqual(result['conflicts'], 0)
        self.assertEqual(result['imported']['deleted'], 0)

    def test_mention_lookup_metadata_is_not_a_new_revision(self):
        vector = struct.pack('<II', 0x1cb5c415, 1)
        mention = struct.pack('<Iii', 0x208e68c9, 0, 5)
        def user(target, access_hash):
            return mention + struct.pack('<Iqq', 0xf21158c6, target, access_hash)
        variants = [(user(1234, 0), user(1234, 9001)),
                    (user(1234, 0), mention + struct.pack('<IIiq', 0x1da448e2, 0x7da07ec9, 17, 1234)),
                    (user(1234, 0), struct.pack('<Iiiq', 0xdc7b1140, 0, 5, 1234)),
                    (user(1, 9001), mention + struct.pack('<I', 0xf7c1b13f))]
        for index, (left, right) in enumerate(variants):
            local = self.row('Alice', message=20 + index, edited=True)
            remote = dict(local)
            local['textEntities'] = vector + left
            remote['textEntities'] = vector + right
            self.insert(self.dest, 'EditedMessage', local)
            self.insert(self.source, 'EditedMessage', remote)
        result = self.merge()
        self.assertEqual(result['imported']['edited'], 0)
        self.assertEqual(result['skipped']['edited'], 4)
        self.assertEqual(result['ambiguities'], 0)
        self.assertEqual([r['textEntities'] for r in self.rows()], [vector + left for left, _ in variants])

    def test_different_mention_targets_remain_distinct_revisions(self):
        prefix = struct.pack('<IIIiiI', 0x1cb5c415, 1, 0x208e68c9, 0, 5, 0xf21158c6)
        local = self.row('Alice', edited=True); remote = dict(local)
        local['textEntities'] = prefix + struct.pack('<qq', 1234, 0)
        remote['textEntities'] = prefix + struct.pack('<qq', 5678, 0)
        self.insert(self.dest, 'EditedMessage', local); self.insert(self.source, 'EditedMessage', remote)
        self.assertEqual(self.merge()['imported']['edited'], 1)
        self.assertEqual(len(self.rows()), 2)

    def test_empty_entity_vector_and_no_blob_are_same_revision(self):
        local = self.row('hello', edited=True); remote = dict(local)
        remote['textEntities'] = struct.pack('<II', 0x1cb5c415, 0)
        self.insert(self.dest, 'EditedMessage', local); self.insert(self.source, 'EditedMessage', remote)
        self.assertEqual(self.merge()['imported']['edited'], 0)

if __name__ == '__main__':
    unittest.main()
