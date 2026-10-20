"""Two synthetic Macs only; never start a client or read real session files."""
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

RESOURCES = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'
sys.path.insert(0, str(RESOURCES))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state
import mac_transfer as transfer
import test_history_archive as archive_fixtures
from test_history_archive import connect


class MacTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / 'data'; self.data.mkdir()
        self.private = self.root / 'private'; self.private.mkdir()
        self.source = self.root / 'old/TG/Group/Source'
        self.dest = self.root / 'new/TG/Target/Destination'
        self.fixture = archive_fixtures.ArchiveTests()
        self.fixture.root = self.root
        self.auth = {}
        for account in (self.source, self.dest):
            original = account / 'TelegramForcePortable/tdata'
            portable = account / '.ayugram/TelegramForcePortable'
            original.mkdir(parents=True)
            (portable / 'tdata').mkdir(parents=True)
            (portable / '.ayugram-inline-test').write_bytes(b'ayugram-inline-isolated-profile-v1\n')
            for directory in (original, portable / 'tdata'):
                for name in ('key_datas', 'settingss', 'encrypted-local-read'):
                    path = directory / name
                    path.write_bytes(('PRIVATE SESSION ' + str(path)).encode())
                    self.auth[path] = path.read_bytes()
            self.fixture.make_db(str(self.db(account)))
            self.prefs(account).write_text(json.dumps({
                'saveDeletedMessages': account == self.source, 'saveMessagesHistory': True,
                'useGlobalGhostMode': True, 'showPeerId': 2, 'translationProvider': 'telegram',
                'semiTransparentDeletedMessages': True,
                'ghostModeSettings': {'0': {'sendReadMessages': False}, '999': {'sendReadMessages': True}},
                'translationApiKey': 'DO-NOT-EXPORT', 'lastFolder': '/private/source/path'}))
            (account / '.ayugram/.common-profile.json').write_text('{"local-marker":"preserved"}')
        self.fixture.insert(self.db(self.source), 'DeletedMessage', self.fixture.row('saved deleted', user=123))
        self.fixture.insert(self.db(self.source), 'EditedMessage', self.fixture.row('older text', user=123, edited=True))
        self.meta = {'notes': {str(self.source): 'source note', str(self.dest): 'keep destination note'},
                     'usernames': {str(self.source): '@synthetic'},
                     'dock_names': {str(self.source): 'Custom Name'},
                     'colors': {str(self.source): '#aabbcc'}, 'pinned': [str(self.source)],
                     'folder_pinned': [str(self.source)], 'proxies': {str(self.source): {'pass': 'SECRET-PROXY'}},
                     'account_ids': {str(self.dest): 'destination-backup-identity'}}
        self.workspaces = {'Research': {'accounts': [str(self.source), '/outside/unselected'], 'icon': '📁', 'created': 'old'}}
        self.meta_file = self.private / 'manager_data.json'
        self.ws_file = self.private / 'manager_workspaces.json'
        self.meta_file.write_text(json.dumps(self.meta))
        self.ws_file.write_text(json.dumps(self.workspaces))
        patches = [mock.patch.object(state, 'ROOT_DIR', str(self.root / 'new/TG')),
                   mock.patch.object(state, 'DATA_DIR', str(self.data)),
                   mock.patch.object(state, 'METADATA_FILE', str(self.meta_file)),
                   mock.patch.object(state, 'WORKSPACES_FILE', str(self.ws_file)),
                   mock.patch.object(state, 'metadata', self.meta),
                   mock.patch.object(state, 'config', {'extra_scan_dirs': [str(self.root / 'old/TG')]}),
                   mock.patch.object(server, '_is_unlocked_no_touch', return_value=True),
                   mock.patch.object(server, '_get_ps_output', return_value='1 /sbin/launchd'),
                   mock.patch.object(transfer.remote_history, '_archive_in_use', return_value=False),
                   mock.patch.object(server, 'open_account', side_effect=AssertionError('No clients may open')),
                   mock.patch.object(server, 'kill_account', side_effect=AssertionError('No clients may close'))]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)
        self.parts = dict(history=True, settings=True, organization=True)

    def tearDown(self):
        for path, content in self.auth.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def db(self, account):
        return account / '.ayugram/TelegramForcePortable/tdata/ayudata.db'

    def prefs(self, account):
        return self.db(account).with_name('ayu_settings.json')

    def package(self, parts=None):
        result = transfer.request('export', {'accounts': [{'path': str(self.source), 'user_id': '123'}],
                                              'include': parts or self.parts})
        self.assertTrue(result['success'], result)
        return result['package']

    def payload(self, package=None, parts=None):
        package = package or self.package()
        return dict(package=package, include=parts or self.parts,
                    mappings=[dict(id=package['accounts'][0]['id'], path=str(self.dest),
                                   user_id='123', confirmed=True)])

    def rows(self, account, table='DeletedMessage'):
        with connect(self.db(account)) as db:
            return db.execute('SELECT text FROM ' + table + ' ORDER BY fakeId').fetchall()

    def resign(self, package):
        package['digest'] = hashlib.sha256(transfer._json({k: v for k, v in package.items() if k != 'digest'})).hexdigest()
        return package

    def test_export_is_read_only_and_contains_only_selected_data(self):
        before = {p: p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        package = self.package()
        text = json.dumps(package)
        for secret in ('PRIVATE SESSION', 'SECRET-PROXY', 'DO-NOT-EXPORT', str(self.root), 'lastFolder', '999'):
            self.assertNotIn(secret, text)
        self.assertEqual(package['accounts'][0]['folder'], 'Group')
        self.assertEqual(len(package['workspaces'][0]['accounts']), 1)
        self.assertEqual(package['accounts'][0]['settings']['ghostModeSettings'], {'0': {'sendReadMessages': False}})
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_empty_destination_import_and_repeat_preserve_auth_and_local_data(self):
        package = self.package()
        result = transfer.request('import', self.payload(package))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['results'][0]['history']['imported'], dict(deleted=1, edited=1))
        self.assertEqual(self.rows(self.dest), [('saved deleted',)])
        self.assertEqual(self.rows(self.dest, 'EditedMessage'), [('older text',)])
        self.assertTrue(json.loads(self.prefs(self.dest).read_text())['saveDeletedMessages'])
        self.assertEqual(json.loads(self.prefs(self.dest).read_text())['translationApiKey'], 'DO-NOT-EXPORT')
        self.assertEqual(self.meta['notes'][str(self.dest)], 'keep destination note')
        self.assertEqual(self.meta['dock_names'][str(self.dest)], 'Custom Name')
        self.assertEqual(self.meta['account_ids'][str(self.dest)], 'destination-backup-identity')
        self.assertIn(str(self.dest), json.loads(self.ws_file.read_text())['Research']['accounts'])
        result = transfer.request('import', self.payload(package))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['results'][0]['history']['imported'], dict(deleted=0, edited=0))
        self.assertEqual(self.meta['pinned'].count(str(self.dest)), 1)
        self.assertEqual(json.loads(self.ws_file.read_text())['Research']['accounts'].count(str(self.dest)), 1)
        self.assertEqual(len(self.rows(self.dest)), 1)
        for path in (self.data / 'Mac Transfer Backups').iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with connect(self.db(self.dest)) as db:
            self.assertEqual(db.execute('SELECT value FROM Unrelated').fetchone()[0], 'untouched')

    def test_imported_preferences_survive_common_profile_application(self):
        package = self.package()
        package['accounts'][0]['settings']['semiTransparentDeletedMessages'] = False
        self.resign(package)
        self.assertTrue(transfer.request('import', self.payload(package))['success'])
        before = self.prefs(self.dest).read_bytes()
        transfer.ayugram._apply_common_profile(str(self.dest), inline_history=True)
        self.assertEqual(self.prefs(self.dest).read_bytes(), before)

    def test_new_export_snapshot_deduplicates_and_local_conflict_wins(self):
        self.fixture.insert(self.db(self.dest), 'DeletedMessage', self.fixture.row('local text', user=123))
        result = transfer.request('import', self.payload())
        self.assertTrue(result['success'], result)
        self.assertEqual(result['results'][0]['history']['conflicts'], 1)
        self.assertEqual(self.rows(self.dest), [('local text',)])
        self.assertTrue(transfer.request('import', self.payload(self.package()))['success'])
        self.assertEqual(len(self.rows(self.dest, 'EditedMessage')), 1)

    def test_selected_settings_only_never_merge_history_or_change_organization(self):
        parts = dict(history=False, settings=True, organization=False)
        before = self.meta_file.read_bytes(), self.ws_file.read_bytes(), self.db(self.dest).read_bytes()
        payload = self.payload(parts=parts)
        payload['mappings'][0].update(confirmed=False, user_id='')
        self.assertTrue(transfer.request('import', payload)['success'])
        self.assertEqual(before, (self.meta_file.read_bytes(), self.ws_file.read_bytes(), self.db(self.dest).read_bytes()))

    def test_organization_only_does_not_need_or_touch_ayugram(self):
        import shutil
        # Delete only synthetic profiles; remove corresponding auth assertions.
        for account in (self.source, self.dest):
            for path in list(self.auth):
                if '.ayugram' in str(path):
                    self.auth.pop(path)
            shutil.rmtree(account / '.ayugram')
        parts = dict(history=False, settings=False, organization=True)
        package = self.package(parts)
        payload = self.payload(package, parts)
        payload['mappings'][0].update(confirmed=False, user_id='')
        self.assertTrue(transfer.request('import', payload)['success'])
        self.assertFalse((self.dest / '.ayugram').exists())
        self.assertEqual(self.meta['dock_names'][str(self.dest)], 'Custom Name')

    def test_missing_and_empty_history_export_are_explicitly_absent(self):
        with connect(self.db(self.source)) as db:
            for table in ('DeletedMessage', 'EditedMessage'):
                db.execute('DELETE FROM ' + table)
        result = transfer.request('export', {'accounts':[{'path':str(self.source),'user_id':''}], 'include':self.parts})
        self.assertTrue(result['success'], result)
        self.assertIsNone(result['package']['accounts'][0]['history'])
        self.assertIsNotNone(result['package']['accounts'][0]['settings'])

    def test_wrong_owner_missing_confirmation_and_duplicate_mapping_write_nothing(self):
        payload = self.payload()
        before = self.db(self.dest).read_bytes(), self.prefs(self.dest).read_bytes(), self.meta_file.read_bytes()
        for changes in ({'confirmed':False}, {'user_id':'124'}, {'user_id':123}, {'path':str(self.root)}):
            bad = copy.deepcopy(payload); bad['mappings'][0].update(changes)
            self.assertFalse(transfer.request('import', bad)['success'])
        duplicate = copy.deepcopy(payload); duplicate['mappings'] *= 2
        self.assertFalse(transfer.request('import', duplicate)['success'])
        self.assertEqual(before, (self.db(self.dest).read_bytes(), self.prefs(self.dest).read_bytes(), self.meta_file.read_bytes()))
        self.fixture.insert(self.db(self.dest), 'DeletedMessage', self.fixture.row(user=999))
        result = transfer.request('import', payload)
        self.assertFalse(result['success'], result)
        self.assertIn('different account', result['message'])

    def test_all_destinations_preflight_before_any_mutation(self):
        package = self.package()
        second = copy.deepcopy(package['accounts'][0]); second['id'] = '55555555-5555-4555-8555-555555555555'
        package['accounts'].append(second); self.resign(package)
        payload = self.payload(package)
        payload['mappings'].append(dict(id=second['id'], path=str(self.source), user_id='123', confirmed=True))
        self.prefs(self.source).write_text('broken json')
        before = self.db(self.dest).read_bytes(), self.prefs(self.dest).read_bytes()
        self.assertFalse(transfer.request('import', payload)['success'])
        self.assertEqual(before, (self.db(self.dest).read_bytes(), self.prefs(self.dest).read_bytes()))

    def test_mutated_checksum_unknown_fields_invalid_preferences_and_history_rejected(self):
        package = self.package()
        mutated = copy.deepcopy(package); mutated['accounts'][0]['name'] = 'changed'
        self.assertFalse(transfer.request('preview', {'package':mutated})['success'])
        cases = []
        for field, value in (('history', []), ('history', 'bad'), ('settings', {'authKey': 'secret'}),
                             ('settings', {'saveDeletedMessages': 'yes'}),
                             ('settings', {'ghostModeSettings': {'123': {}}}),
                             ('metadata', {'proxies': {'pass':'secret'}}),
                             ('metadata', {'colors':'url(https://bad)'})):
            bad = copy.deepcopy(package); bad['accounts'][0][field] = value; cases.append(self.resign(bad))
        bad = copy.deepcopy(package); bad['sessions'] = {}; cases.append(self.resign(bad))
        bad = copy.deepcopy(package); bad['workspaces'][0]['accounts'] = ['unknown']; cases.append(self.resign(bad))
        for bad in cases:
            self.assertFalse(transfer.request('preview', {'package':bad})['success'])
        self.assertEqual(self.rows(self.dest), [])

    def test_size_bounds_and_unsupported_media_fail_without_silent_loss(self):
        package = self.package()
        with mock.patch.object(transfer, 'MAX_BYTES', 100):
            self.assertFalse(transfer.request('preview', {'package':package})['success'])
        with connect(self.db(self.source)) as db:
            db.execute("UPDATE DeletedMessage SET mediaPath='/private/photo.jpg'")
        result = transfer.request('export', {'accounts':[{'path':str(self.source),'user_id':'123'}], 'include':self.parts})
        self.assertFalse(result['success'])
        self.assertIn('Unsupported archive media', result['message'])
        self.assertEqual(self.rows(self.source), [('saved deleted',)])

    def test_settings_export_still_works_when_history_schema_is_unsupported(self):
        with connect(self.db(self.source)) as db:
            db.execute('UPDATE SchemaVersion SET version=999')
        catalog = transfer.request('catalog', {'path':str(self.source)})
        self.assertTrue(catalog['success'])
        self.assertFalse(catalog['history_available'])
        self.assertTrue(catalog['settings_available'])
        self.assertIn('History cannot be transferred', catalog['message'])
        package = self.package(dict(history=False, settings=True, organization=True))
        self.assertIsNotNone(package['accounts'][0]['settings'])

    def test_busy_locked_unknown_process_and_linked_profile_fail_closed(self):
        package = self.package()
        for target, name, value in ((server, '_is_unlocked_no_touch', False),
                                     (server, '_get_ps_output', None),
                                     (server, '_client_pid_from_ps_output', 99),
                                     (transfer.remote_history, '_archive_in_use', True)):
            with mock.patch.object(target, name, return_value=value):
                self.assertFalse(transfer.request('import', self.payload(package))['success'])
        original = self.prefs(self.dest).read_bytes()
        self.prefs(self.dest).unlink(); self.prefs(self.dest).symlink_to(self.prefs(self.source))
        self.assertFalse(transfer.request('import', self.payload(package))['success'])
        self.prefs(self.dest).unlink(); self.prefs(self.dest).write_bytes(original)
        linked = self.root / 'linked-settings'; linked.write_bytes(original)
        self.prefs(self.dest).unlink(); os.link(linked, self.prefs(self.dest))
        self.assertFalse(transfer.request('import', self.payload(package))['success'])
        self.assertEqual(self.rows(self.dest), [])

    def test_account_lock_and_transfer_lock_reject_concurrent_work(self):
        payload = self.payload()
        lock = state._account_path_lock(str(self.dest))
        lock.acquire()
        try:
            results = []
            thread = threading.Thread(target=lambda:results.append(transfer.request('import', payload)))
            thread.start(); thread.join(5)
            self.assertFalse(results[0]['success'])
        finally:
            lock.release()
        transfer._transfer_lock.acquire()
        try:
            self.assertFalse(transfer.request('preview', {'package':payload['package']})['success'])
        finally:
            transfer._transfer_lock.release()

    def test_settings_write_failure_rolls_back_and_reports_committed_history(self):
        payload = self.payload()
        before = self.prefs(self.dest).read_bytes()
        real = transfer._atomic_bytes
        failed = False
        def write(path, content):
            nonlocal failed
            if path.name == '.common-profile.json' and not failed:
                failed = True
                raise OSError('synthetic disk full')
            return real(path, content)
        with mock.patch.object(transfer, '_atomic_bytes', side_effect=write):
            result = transfer.request('import', payload)
        self.assertFalse(result['success']); self.assertTrue(result['partial'], result)
        self.assertEqual(self.prefs(self.dest).read_bytes(), before)
        self.assertEqual(self.rows(self.dest), [('saved deleted',)])
        self.assertIn('already merged', result['results'][0]['message'])
        self.assertTrue(transfer.request('import', payload)['success'])
        self.assertEqual(len(self.rows(self.dest)), 1)

    def test_organization_failure_rolls_back_files_and_in_memory_metadata(self):
        payload = self.payload(parts=dict(history=False, settings=False, organization=True))
        before = self.meta_file.read_bytes(), self.ws_file.read_bytes(), copy.deepcopy(self.meta)
        real = transfer._atomic_bytes
        def write(path, content):
            if path == self.ws_file:
                raise OSError('synthetic write failure')
            return real(path, content)
        with mock.patch.object(transfer, '_atomic_bytes', side_effect=write):
            result = transfer.request('import', payload)
        self.assertFalse(result['success'])
        self.assertFalse(result['partial'])
        self.assertFalse(result['results'][0]['success'])
        self.assertEqual(before, (self.meta_file.read_bytes(), self.ws_file.read_bytes(), self.meta))

    def test_history_closed_recheck_rolls_back_transaction(self):
        payload = self.payload(parts=dict(history=True, settings=False, organization=False))
        calls = 0
        def closed(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValueError('Client started externally')
            return True
        with mock.patch.object(transfer, '_closed', side_effect=closed):
            result = transfer.request('import', payload)
        self.assertFalse(result['success'])
        self.assertEqual(self.rows(self.dest), [])

    def test_empty_archive_opt_in_does_not_weaken_remote_sync_default(self):
        package = self.package()
        entry = package['accounts'][0]
        with self.assertRaisesRegex(ValueError, 'absent'):
            transfer.history_archive.merge_archive(self.db(self.dest), entry['history'], entry['id'],
                                                   self.root/'history.db', lambda:True)
        self.fixture.insert(self.db(self.dest), 'DeletedMessage', self.fixture.row(user=999))
        with self.assertRaisesRegex(ValueError, 'absent'):
            transfer.history_archive.merge_archive(self.db(self.dest), entry['history'], entry['id'],
                                                   self.root/'history.db', lambda:True, allow_empty=True)

    def test_api_routes_require_token_and_unlock(self):
        httpd = server.ThreadedHTTPServer(('127.0.0.1', 0), server.RequestHandler)
        self.addCleanup(httpd.server_close)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True); worker.start()
        self.addCleanup(httpd.shutdown)
        def post(path):
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            try:
                conn.request('POST', path, '{}', {'Content-Type':'application/json'})
                response = conn.getresponse(); body = response.read()
                return response.status, body
            finally:
                conn.close()
        for action in ('catalog','export','preview','import'):
            path = '/api/mac-transfer/' + action
            self.assertEqual(post(path)[0], 404)
            with mock.patch.object(server, '_check_and_touch_unlocked', return_value=False):
                self.assertEqual(post(server.ROUTE_PREFIX + path)[0], 423)
            with mock.patch.object(server, '_check_and_touch_unlocked', return_value=True):
                code, body = post(server.ROUTE_PREFIX + path)
                self.assertEqual(code, 200)
                self.assertFalse(json.loads(body)['success'])

        with mock.patch.object(server, '_check_and_touch_unlocked', return_value=True):
            for route, size in (('/api/mac-transfer/import', transfer.MAX_BYTES + 1024 * 1024 + 1),
                                ('/api/mac-transfer/preview', transfer.MAX_BYTES + 1024 * 1024 + 1),
                                ('/api/mac-transfer/export', 10 * 1024 * 1024 + 1),
                                ('/api/config', 10 * 1024 * 1024 + 1)):
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
                try:
                    conn.request('POST', server.ROUTE_PREFIX + route, headers={'Content-Length':str(size)})
                    response = conn.getresponse(); response.read()
                    self.assertEqual(response.status, 413)
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
