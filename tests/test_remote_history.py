"""Orchestration uses only synthetic profiles and mocked SSH; no clients launch."""
import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
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
import remote_history as history
_REAL_CATALOG = history.history_archive.catalog
_REAL_MERGE = history.history_archive.merge_archive
_REAL_DIGEST = history.history_archive.imported_digest


class RemoteHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data = self.root / 'data'; self.data.mkdir()
        self.account = self.root / 'TG' / 'Synthetic'
        original = self.account / 'TelegramForcePortable/tdata'
        original.mkdir(parents=True)
        self.key = original / 'key_datas'; self.key.write_bytes(b'untouched Telegram')
        self.portable = self.account / '.ayugram/TelegramForcePortable'
        self.portable.joinpath('tdata').mkdir(parents=True)
        (self.portable / '.ayugram-inline-test').write_bytes(b'ayugram-inline-isolated-profile-v1\n')
        self.db = self.portable / 'tdata/ayudata.db'; self.db.write_bytes(b'synthetic archive')
        self.ayu_key = self.db.with_name('key_datas'); self.ayu_key.write_bytes(b'untouched AyuGram')
        self.settings = self.db.with_name('ayu_settings.json'); self.settings.write_text('{"ghost":true}')
        self.cloud = dict(schema=1, project='test-project', zone='us-central1-a', instance='test-vm')
        self.catalog = dict(protocol=1, schema_version=1, accounts=[dict(user_id='123', deleted=2, edited=3)])
        self.result = dict(digest='a'*64, user_id='123', imported=dict(deleted=2,edited=3),
                           skipped=dict(deleted=0,edited=0), conflicts=0, ambiguities=0, unchanged=False)
        patches = [mock.patch.object(state, 'DATA_DIR', str(self.data)),
                   mock.patch.object(state, 'ROOT_DIR', str(self.root/'TG')),
                   mock.patch.object(state, 'config', {'extra_scan_dirs':[]}),
                   mock.patch.object(server, '_is_unlocked_no_touch', return_value=True),
                   mock.patch.object(server, '_get_ps_output', return_value='1 /sbin/launchd'),
                   mock.patch.object(history, '_archive_in_use', return_value=False),
                   mock.patch.object(history.remote_ayugram, '_config', return_value=self.cloud),
                   mock.patch.object(history.remote_ayugram, 'history_request', return_value=self.catalog),
                   mock.patch.object(history.history_archive, 'catalog', return_value=self.catalog),
                   mock.patch.object(history.history_archive, 'imported_digest', return_value='', create=True),
                   mock.patch.object(history.history_archive, 'merge_archive', return_value=self.result)]
        for patcher in patches:
            value = patcher.start(); self.addCleanup(patcher.stop)
        self.path = str(self.account)

    def tearDown(self):
        self.assertEqual(self.key.read_bytes(), b'untouched Telegram')
        self.assertEqual(self.ayu_key.read_bytes(), b'untouched AyuGram')
        self.assertEqual(self.settings.read_text(), '{"ghost":true}')

    def enable(self):
        response = history.request('configure', {'path': self.path, 'user_id': '123', 'confirmed': True})
        self.assertTrue(response['success'], response)
        return response

    def test_disabled_default_never_connects_or_writes(self):
        self.assertEqual(history.status()['bindings'], [])
        self.assertIsNone(history.before_open(self.path))
        history.run_pass()
        history.remote_ayugram.history_request.assert_not_called()
        history.history_archive.merge_archive.assert_not_called()
        self.assertEqual(list(self.data.iterdir()), [])

    def test_binding_requires_explicit_confirmation_and_matching_id(self):
        for payload in ({'path':self.path,'user_id':'123'},
                        {'path':self.path,'user_id':123,'confirmed':True},
                        {'path':self.path,'user_id':'124','confirmed':True}):
            self.assertFalse(history.request('configure', payload)['success'])
        history.history_archive.merge_archive.assert_not_called()
        self.assertEqual(history.status()['bindings'], [])

    def test_binding_is_private_durable_and_never_imports_until_requested(self):
        self.enable()
        binding = history.status()['bindings'][0]
        self.assertEqual(binding['user_id'], '123')
        self.assertTrue(binding['enabled'])
        self.assertEqual((self.data/'remote_history.json').stat().st_mode & 0o777, 0o600)
        history.history_archive.merge_archive.assert_not_called()
        self.enable()
        self.assertEqual(history.status()['bindings'][0]['id'], binding['id'])

    def test_missing_original_or_custom_marker_and_linked_paths_are_rejected(self):
        marker = self.portable / '.ayugram-inline-test'
        marker.unlink()
        self.assertFalse(history.request('catalog', {'path':self.path})['success'])
        marker.write_bytes(b'ayugram-inline-isolated-profile-v1\n')
        self.db.unlink(); self.db.symlink_to(self.key)
        self.assertFalse(history.request('catalog', {'path':self.path})['success'])
        self.assertFalse(history.request('catalog', {'path':str(self.root)})['success'])
        history.remote_ayugram.history_request.assert_not_called()

    def test_running_client_defers_without_network_or_mutations(self):
        self.enable(); history.remote_ayugram.history_request.reset_mock()
        with mock.patch.object(server, '_client_pid_from_ps_output', return_value=77):
            result = history.request('sync', {'path':self.path})
        self.assertEqual(result['state'], 'deferred')
        history.remote_ayugram.history_request.assert_not_called()
        history.history_archive.merge_archive.assert_not_called()

    def test_process_unknown_fails_closed(self):
        self.enable()
        with mock.patch.object(server, '_get_ps_output', return_value=None):
            self.assertFalse(history.request('sync', {'path':self.path})['success'])
        history.history_archive.merge_archive.assert_not_called()

    def test_sync_is_bound_and_preserves_settings_and_auth(self):
        self.enable()
        history.remote_ayugram.history_request.return_value = {'digest':'a'*64, 'user_id':'123', 'unchanged':False}
        result = history.request('sync', {'path':self.path})
        self.assertTrue(result['success'], result)
        args = history.history_archive.merge_archive.call_args
        self.assertEqual(Path(args.args[0]), self.db)
        self.assertTrue(args.kwargs['check_closed']())
        self.assertTrue(str(args.kwargs['backup_path']).startswith(str(self.data)))
        self.assertGreater(history.status()['bindings'][0]['last_success'], 0)

    def test_wrong_owner_response_and_changed_host_do_not_import(self):
        self.enable()
        history.remote_ayugram.history_request.return_value = {'user_id':'456'}
        self.assertFalse(history.request('sync', {'path':self.path})['success'])
        history.remote_ayugram._config.return_value = {**self.cloud,'instance':'other-vm'}
        self.assertFalse(history.request('sync', {'path':self.path})['success'])
        history.history_archive.merge_archive.assert_not_called()

    def test_unchanged_poll_retains_import_counts_and_conflict_notice(self):
        self.enable()
        history.remote_ayugram.history_request.return_value = {'user_id':'123'}
        history.history_archive.merge_archive.return_value = {**self.result, 'conflicts':1}
        history.request('sync', {'path':self.path})
        history.history_archive.merge_archive.return_value = {**self.result, 'unchanged':True, 'conflicts':0,
                                                             'imported':{'deleted':0,'edited':0}}
        history.request('sync', {'path':self.path})
        binding = history.status()['bindings'][0]
        self.assertEqual(binding['state'], 'warning')
        self.assertEqual(binding['counts']['conflicts'], 1)
        self.assertEqual(binding['counts']['imported'], self.result['imported'])

    def test_missing_archive_is_visible_to_background_monitor_and_can_be_disabled(self):
        self.enable(); self.db.unlink()
        history.run_pass()
        self.assertEqual(history.status()['bindings'][0]['state'], 'error')
        self.assertTrue(history.request('disable', {'path':self.path})['success'])

    def test_destination_receipts_not_config_are_digest_authority(self):
        self.enable()
        history.remote_ayugram.history_request.return_value = {'digest':'a'*64, 'user_id':'123', 'unchanged':False}
        for _ in range(2):
            self.assertTrue(history.request('sync', {'path':self.path})['success'])
        # A stale config after database restore cannot suppress a needed import.
        for call in history.remote_ayugram.history_request.call_args_list:
            if call.args[0] == 'history-export':
                self.assertEqual(call.args[1].get('known_digest', ''), '')

    def test_disable_retains_history_and_receipt_identity(self):
        self.enable(); identity = history.status()['bindings'][0]['id']
        self.assertTrue(history.request('disable', {'path':self.path})['success'])
        self.assertFalse(history.status()['bindings'][0]['enabled'])
        self.assertEqual(self.db.read_bytes(), b'synthetic archive')
        history.remote_ayugram.history_request.reset_mock()
        self.assertIsNone(history.before_open(self.path)); history.run_pass()
        history.remote_ayugram.history_request.assert_not_called()
        self.enable(); self.assertEqual(history.status()['bindings'][0]['id'], identity)

    def test_locked_monitor_does_no_work_and_commit_guard_rechecks_lock(self):
        self.enable()
        history.remote_ayugram.history_request.reset_mock()
        with mock.patch.object(server, '_is_unlocked_no_touch', return_value=False):
            history.run_pass()
            self.assertFalse(history.request('sync', {'path':self.path})['success'])
        history.remote_ayugram.history_request.assert_not_called()

    def test_prelaunch_outage_keeps_local_archive_available(self):
        self.enable()
        history.remote_ayugram.history_request.side_effect = ValueError('VPS unavailable')
        notice = history.before_open(self.path)
        self.assertIn('VPS unavailable', notice)
        history.history_archive.merge_archive.assert_not_called()
        self.assertEqual(history.status()['bindings'][0]['state'], 'error')

    def test_foreign_lock_defers_and_monitor_retries_later(self):
        self.enable()
        lock = mock.Mock(); lock.acquire.return_value = False
        with mock.patch.object(state, '_account_path_lock', return_value=lock):
            self.assertEqual(history.request('sync', {'path':self.path})['state'], 'deferred')
        history.history_archive.merge_archive.assert_not_called()

    def test_config_symlink_or_world_readable_fails_without_following(self):
        target = self.data/'remote_history.json'
        target.symlink_to(self.settings)
        self.assertFalse(history.status()['success'])
        target.unlink(); target.write_text('{"schema":1,"bindings":{}}'); target.chmod(0o644)
        self.assertFalse(history.status()['success'])

    def test_real_sqlite_roundtrip_replay_and_offline_catchup(self):
        import test_history_archive as fixtures
        fixture = fixtures.ArchiveTests()
        fixture.root = self.root
        source = fixture.make_db('vps.db')
        self.db.unlink(); fixture.make_db(str(self.db.relative_to(self.root)))
        fixture.insert(self.db, 'DeletedMessage', fixture.row('local text', user=123, message=1))
        fixture.insert(source, 'DeletedMessage', fixture.row('remote text', user=123, message=2))
        with fixtures.connect(self.db) as db:
            db.execute('CREATE TABLE LocalReadMarker(value TEXT)')
            db.execute("INSERT INTO LocalReadMarker VALUES('unchanged')")
        def exchange(action, payload, **kwargs):
            if action == 'history-catalog':
                return _REAL_CATALOG(source)
            return history.history_archive.export_archive(source, payload['user_id'], payload['known_digest'])
        with mock.patch.object(history.history_archive, 'catalog', _REAL_CATALOG), \
                mock.patch.object(history.history_archive, 'imported_digest', _REAL_DIGEST), \
                mock.patch.object(history.history_archive, 'merge_archive', _REAL_MERGE), \
                mock.patch.object(history.remote_ayugram, 'history_request', side_effect=exchange):
            self.enable()
            first = history.request('sync', {'path':self.path})
            self.assertTrue(first['success'], first)
            self.assertEqual(first['imported']['deleted'], 1)
            second = history.request('sync', {'path':self.path})
            self.assertTrue(second['unchanged'])
            fixture.insert(source, 'EditedMessage', fixture.row('offline edit', user=123, message=2, edited=True))
            third = history.request('sync', {'path':self.path})
            self.assertEqual(third['imported']['edited'], 1)
            history.request('sync', {'path':self.path})
        with fixtures.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM DeletedMessage').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM EditedMessage').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT value FROM LocalReadMarker').fetchone()[0], 'unchanged')
        backups = list((self.data/'History Sync Backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_empty_vps_waits_for_matching_history_before_first_import(self):
        import test_history_archive as fixtures
        fixture = fixtures.ArchiveTests()
        fixture.root = self.root
        source = fixture.make_db('new-vps.db')
        self.db.unlink(); fixture.make_db(str(self.db.relative_to(self.root)))
        fixture.insert(self.db, 'DeletedMessage', fixture.row('local text', user=123, message=1))
        original = self.db.read_bytes()
        exported_ids = []

        def exchange(action, payload, **kwargs):
            if action == 'history-catalog':
                return _REAL_CATALOG(source)
            exported_ids.append(payload['user_id'])
            return history.history_archive.export_archive(source, payload['user_id'], payload['known_digest'])

        with mock.patch.object(history.history_archive, 'catalog', _REAL_CATALOG), \
                mock.patch.object(history.history_archive, 'imported_digest', _REAL_DIGEST), \
                mock.patch.object(history.history_archive, 'merge_archive', _REAL_MERGE), \
                mock.patch.object(history.remote_ayugram, 'history_request', side_effect=exchange):
            enabled = self.enable()
            self.assertEqual(enabled['binding']['state'], 'waiting')
            self.assertEqual(history.status()['bindings'][0]['last_success'], 0)
            waiting = history.request('sync', {'path': self.path})
            self.assertTrue(waiting['success'], waiting)
            self.assertEqual(waiting['state'], 'waiting')
            self.assertEqual(self.db.read_bytes(), original)
            self.assertEqual(exported_ids, [])
            self.assertFalse((self.data / 'History Sync Backups').exists())

            history.request('disable', {'path': self.path})
            history.run_pass()
            self.assertFalse(history.status()['bindings'][0]['enabled'])
            self.assertEqual(exported_ids, [])
            self.enable()

            fixture.insert(source, 'DeletedMessage', fixture.row('different owner', user=456, message=2))
            wrong = history.request('sync', {'path': self.path})
            self.assertFalse(wrong['success'], wrong)
            self.assertEqual(history.status()['bindings'][0]['state'], 'error')
            self.assertEqual(exported_ids, [])
            self.assertEqual(self.db.read_bytes(), original)

            self.assertFalse(history.request('configure', {
                'path': self.path, 'user_id': '123', 'confirmed': True})['success'])

            fixture.insert(source, 'DeletedMessage', fixture.row('matching owner', user=123, message=3))
            imported = history.request('sync', {'path': self.path})
            self.assertTrue(imported['success'], imported)
            self.assertEqual(imported['imported']['deleted'], 1)
            replay = history.request('sync', {'path': self.path})
            self.assertTrue(replay['unchanged'])
            self.assertGreater(history.status()['bindings'][0]['last_success'], 0)
        with fixtures.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT userId,messageId FROM DeletedMessage ORDER BY messageId').fetchall(),
                             [(123, 1), (123, 3)])
        self.assertEqual(set(exported_ids), {'123'})

    def test_empty_vps_still_requires_local_id_and_confirmation(self):
        history.remote_ayugram.history_request.return_value = {**self.catalog, 'accounts': []}
        for payload in ({'path': self.path, 'user_id': '123'},
                        {'path': self.path, 'user_id': '456', 'confirmed': True}):
            self.assertFalse(history.request('configure', payload)['success'])
        self.assertEqual(history.status()['bindings'], [])

    def test_routes_require_token_lock_and_status_does_not_extend_idle_time(self):
        httpd = server.ThreadedHTTPServer(('127.0.0.1', 0), server.RequestHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close); self.addCleanup(httpd.shutdown)
        with mock.patch.dict(server.config, {'lock_password_hash':'enabled','lock_timeout_minutes':5}), \
                mock.patch.object(server, '_lock_unlocked_at', 0.0), \
                mock.patch.object(server, '_is_unlocked_no_touch', return_value=False):
            for action in ('status','catalog','configure','sync','disable'):
                for prefix, expected in (('',404),(server.ROUTE_PREFIX,423)):
                    conn = http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
                    try:
                        conn.request('GET' if action == 'status' else 'POST',prefix+'/api/remote-history/'+action,body='{}')
                        response = conn.getresponse(); response.read()
                        self.assertEqual(response.status, expected)
                    finally: conn.close()
        history.remote_ayugram.history_request.assert_not_called()
        with mock.patch.object(server, '_check_and_touch_unlocked') as touch:
            conn = http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=5)
            try:
                conn.request('GET',server.ROUTE_PREFIX+'/api/remote-history/status')
                response = conn.getresponse(); response.read(); self.assertEqual(response.status,200)
            finally: conn.close()
            touch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
