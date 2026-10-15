import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state
import backups


class BackupPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.account = self.root / 'Account'
        self.telegram = self.account / 'TelegramForcePortable/tdata'
        self.ayu = self.account / '.ayugram/TelegramForcePortable/tdata'
        self.telegram.mkdir(parents=True)
        self.ayu.mkdir(parents=True)
        self.write(self.telegram, 'key_datas', b'fixture telegram key')
        self.write(self.ayu, 'key_datas', b'fixture ayugram key')
        old_meta = copy.deepcopy(state.metadata)
        self.addCleanup(lambda: (state.metadata.clear(), state.metadata.update(old_meta)))
        for module in (server, state):
            for key in ('ROOT_DIR', 'DATA_DIR'):
                patch = mock.patch.object(module, key, str(self.root))
                patch.start()
                self.addCleanup(patch.stop)
        for key in ('METADATA_FILE',):
            patch = mock.patch.object(state, key, str(self.root / 'metadata.json'))
            patch.start()
            self.addCleanup(patch.stop)
        for target, value in [('server._get_ps_output', '123 /unrelated/process'),
                              ('server.scan_accounts', []), ('backups.prune_backups', None)]:
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, root, name, data=b'preserved'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def make_backup(self):
        result = backups.backup_account(str(self.account), 'Account')
        self.assertTrue(result[0], result[1])
        return Path(result[2])

    def test_backup_preserves_both_profiles_wal_preferences_and_local_attachments(self):
        names = ['key_datas', 'settingss', 'ayudata.db', 'ayudata.db-wal',
                 'ayudata.db-shm', 'custom/media_cache/history', 'user_data/custom/cache/saved']
        for root in (self.telegram, self.ayu):
            for name in names:
                self.write(root, name, (str(root) + name).encode())
        marker = self.account / '.ayugram/.common-profile.json'
        marker.write_text('{"source": "fixture"}')
        backup = self.make_backup()
        for source, destination in ((self.telegram, backup / 'tdata'), (self.ayu, backup / 'ayugram/tdata')):
            for name in names:
                self.assertEqual(self.digest(source / name), self.digest(destination / name), name)
        self.assertEqual(self.digest(marker), self.digest(backup / 'ayugram/.common-profile.json'))
        self.assertEqual(json.loads((backup / 'backup.json').read_text())['clients'], ['Telegram', 'AyuGram'])
        listed = next(item for item in backups.list_backups() if item['backup_path'] == str(backup))
        self.assertEqual(listed['clients'], ['Telegram', 'AyuGram'])
        self.assertEqual(listed['size'], state.get_folder_size(str(backup / 'tdata')) + state.get_folder_size(str(backup / 'ayugram')))

    def test_legacy_ayu_backup_keeps_allowlisted_local_media(self):
        self.write(self.telegram, 'ayudata.db', b'fixture history')
        media = self.write(self.telegram, 'user_data/media_cache/local-only', b'local attachment')
        backup = self.make_backup()
        self.assertTrue((backup / 'tdata/user_data/media_cache/local-only').is_file())
        self.assertEqual((backup / 'tdata/user_data/media_cache/local-only').read_bytes(), media.read_bytes())

    def test_backup_inventory_detects_changed_or_missing_files_before_restore(self):
        backup = self.make_backup()
        self.write(self.telegram, 'key_datas', b'current session')
        path = backup / 'tdata/key_datas'
        for mutation in ('change', 'remove'):
            with self.subTest(mutation=mutation):
                if mutation == 'change':
                    path.write_bytes(b'x' * path.stat().st_size)
                else:
                    path.unlink()
                result = backups.restore_backup(str(backup), str(self.account))
                self.assertFalse(result[0])
                self.assertEqual((self.telegram / 'key_datas').read_bytes(), b'current session')
                self.assertFalse(list(self.telegram.parent.glob('tdata.bak.*')))

    def test_incomplete_copy_never_publishes_or_prunes(self):
        first = self.make_backup()
        copy_profile = backups._copy_tdata_excluding_cache
        def incomplete(source, destination, *args, **kwargs):
            result = copy_profile(source, destination, *args, **kwargs)
            if source == str(self.telegram):
                (Path(destination) / 'key_datas').unlink()
            return result
        with mock.patch.object(backups, '_copy_tdata_excluding_cache', side_effect=incomplete), \
             mock.patch.object(backups, 'prune_backups') as prune:
            result = backups.backup_account(str(self.account), 'Account')
        self.assertFalse(result[0])
        self.assertTrue(first.is_dir())
        self.assertEqual(len(backups.list_backups()), 1)
        prune.assert_not_called()

    def test_legacy_empty_restore_is_rejected_but_keyed_restore_still_works(self):
        backup = self.root / 'Backups/legacy/Account'
        (backup / 'tdata').mkdir(parents=True)
        self.assertFalse(backups.restore_backup(str(backup), str(self.account))[0])
        self.assertEqual((self.telegram / 'key_datas').read_bytes(), b'fixture telegram key')
        self.write(backup / 'tdata', 'key_data1', b'legacy session')
        self.assertTrue(backups.restore_backup(str(backup), str(self.account))[0])
        self.assertEqual((self.telegram / 'key_data1').read_bytes(), b'legacy session')

    def test_explicit_fresh_ayu_login_backup_can_restore_without_a_key(self):
        (self.ayu / 'key_datas').unlink()
        marker = self.account / '.ayugram/.new-login.json'
        marker.write_text('{"new_login": true}')
        backup = self.make_backup()
        marker.unlink()
        self.write(self.ayu, 'key_datas', b'current ayu session')
        self.assertTrue(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])
        self.assertFalse((self.ayu / 'key_datas').exists())
        self.assertEqual(json.loads(marker.read_text()), {'new_login': True})

    def test_ayu_marker_crash_keeps_recoverable_transaction(self):
        marker = self.account / '.ayugram/.new-login.json'
        marker.write_text('{"new_login": true}')
        self.write(self.ayu, 'history', b'backup history')
        backup = self.make_backup()
        marker.unlink()
        self.write(self.ayu, 'history', b'current history')
        save = state._save_json_atomic
        def crash(path, value):
            if path == str(marker):
                raise KeyboardInterrupt('crash while installing profile marker')
            return save(path, value)
        with mock.patch.object(state, '_save_json_atomic', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                backups.restore_backup(str(backup), str(self.account), 'AyuGram')
        journal = self.ayu.parent / '.tdata-restore.json'
        self.assertTrue(journal.exists())
        self.assertTrue(backups.recover_interrupted_restore(str(self.account), 'AyuGram'))
        self.assertEqual((self.ayu / 'history').read_bytes(), b'backup history')
        self.assertEqual(json.loads(marker.read_text()), {'new_login': True})
        self.assertFalse(journal.exists())

    def test_marker_write_failure_keeps_journal_until_recovery_succeeds(self):
        marker = self.account / '.ayugram/.common-profile.json'
        marker.write_text('{"fingerprint": "backup"}')
        backup = self.make_backup()
        marker.write_text('{"fingerprint": "current"}')
        save = state._save_json_atomic
        def fail(path, value):
            if path == str(marker):
                raise OSError('fixture disk error')
            return save(path, value)
        with mock.patch.object(state, '_save_json_atomic', side_effect=fail):
            self.assertFalse(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])
        self.assertTrue((self.ayu.parent / '.tdata-restore.json').exists())
        self.assertTrue(backups.recover_interrupted_restore(str(self.account), 'AyuGram'))
        self.assertEqual(json.loads(marker.read_text()), {'fingerprint': 'backup'})

    def test_backup_copy_has_independent_inodes_and_changes_do_not_touch_live_data(self):
        backup = self.make_backup()
        copy = backup / 'ayugram/tdata/key_datas'
        self.assertNotEqual(copy.stat().st_ino, (self.ayu / 'key_datas').stat().st_ino)
        copy.write_bytes(b'changed backup')
        self.assertEqual((self.ayu / 'key_datas').read_bytes(), b'fixture ayugram key')

    def test_invalid_inventory_never_falls_back_to_unverified_legacy_restore(self):
        backup = self.make_backup()
        manifest_file = backup / 'backup.json'
        original = json.loads(manifest_file.read_text())
        for integrity in (None, [], {'version': 99, 'files': {}}, {'version': 1, 'files': {}}):
            with self.subTest(integrity=integrity):
                manifest = dict(original, integrity=integrity)
                manifest_file.write_text(json.dumps(manifest))
                self.assertFalse(backups.restore_backup(str(backup), str(self.account))[0])
                self.assertEqual((self.telegram / 'key_datas').read_bytes(), b'fixture telegram key')

    def test_corrupt_retained_backup_blocks_pruning_valid_old_version(self):
        with mock.patch.dict(state.config, {'backup_keep_per_account': 1}):
            old = self.make_backup()
            newest = self.make_backup()
            (newest / 'ayugram/tdata/key_datas').write_bytes(b'corrupt')
            account_id = json.loads((newest / 'backup.json').read_text())['account_id']
            self._prune(account_id)
        self.assertTrue(old.is_dir())
        self.assertTrue(newest.is_dir())

    def test_pruning_preserves_latest_legacy_ayu_history_backup(self):
        account_id = '12345678-1234-1234-1234-123456789abc'
        paths = []
        for day in (1, 2):
            backup = self.root / 'Backups' / f'2026-10-{day:02}' / account_id
            self.write(backup / 'tdata', 'key_datas', b'fixture key')
            if day == 1:
                self.write(backup / 'tdata', 'ayudata.db', b'only saved history')
            self.write(backup, 'backup.json', json.dumps({'account_id': account_id,
                       'account_name': 'Account', 'created_at': str(day)}).encode())
            paths.append(backup)
        with mock.patch.dict(state.config, {'backup_keep_per_account': 1}):
            self._prune(account_id)
        self.assertTrue(paths[0].is_dir())
        self.assertTrue(paths[1].is_dir())

    def test_corrupted_restore_staging_never_changes_live_session(self):
        backup = self.make_backup()
        copy_profile = backups._copy_tdata_excluding_cache
        def corrupt(source, destination, *args, **kwargs):
            result = copy_profile(source, destination, *args, **kwargs)
            (Path(destination) / 'key_datas').write_bytes(b'injected staged corruption')
            return result
        with mock.patch.object(backups, '_copy_tdata_excluding_cache', side_effect=corrupt):
            self.assertFalse(backups.restore_backup(str(backup), str(self.account))[0])
        self.assertEqual((self.telegram / 'key_datas').read_bytes(), b'fixture telegram key')
        self.assertFalse(list(self.telegram.parent.glob('tdata.bak.*')))

    def test_ayu_restore_rollback_keeps_previous_markers(self):
        marker = self.account / '.ayugram/.common-profile.json'
        marker.write_text('{"fingerprint": "backup"}')
        backup = self.make_backup()
        marker.write_text('{"fingerprint": "current"}')
        self.write(self.ayu, 'key_datas', b'current key')
        rename = os.rename
        def fail_install(source, destination):
            if source == str(self.ayu) + '.new':
                raise OSError('fixture install failure')
            return rename(source, destination)
        with mock.patch.object(backups.os, 'rename', side_effect=fail_install):
            self.assertFalse(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])
        self.assertEqual((self.ayu / 'key_datas').read_bytes(), b'current key')
        self.assertEqual(json.loads(marker.read_text()), {'fingerprint': 'current'})
        self.assertFalse((self.ayu.parent / '.tdata-restore.json').exists())

    def test_unsafe_marker_temp_file_blocks_restore_without_touching_target(self):
        backup = self.make_backup()
        protected = self.root / 'protected'
        protected.write_bytes(b'keep')
        os.symlink(protected, self.account / '.ayugram/.common-profile.json.tmp')
        self.assertFalse(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])
        self.assertEqual(protected.read_bytes(), b'keep')
        self.assertEqual((self.ayu / 'key_datas').read_bytes(), b'fixture ayugram key')

    def test_hardlinked_history_refuses_backup_without_touching_either_copy(self):
        original = self.write(self.ayu, 'ayudata.db', b'fixture history')
        other = self.root / 'other-history'
        os.link(original, other)
        self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
        self.assertEqual(original.read_bytes(), b'fixture history')
        self.assertEqual(other.read_bytes(), b'fixture history')

    def test_clone_and_rsync_failure_falls_back_without_copying_safe_cache(self):
        self.write(self.telegram, 'user_data/media_cache/download', b'regenerable')
        run = subprocess.run
        def no_clone_or_rsync(command, *args, **kwargs):
            if command[:2] == ['cp', '-cR']:
                return subprocess.CompletedProcess(command, 1, b'', b'clone unsupported')
            if command[0] == 'rsync':
                raise FileNotFoundError('fixture rsync unavailable')
            return run(command, *args, **kwargs)
        with mock.patch.object(backups.sys, 'platform', 'darwin'), \
             mock.patch.object(backups.subprocess, 'run', side_effect=no_clone_or_rsync):
            backup = self.make_backup()
        self.assertEqual((backup / 'tdata/key_datas').read_bytes(), b'fixture telegram key')
        self.assertFalse((backup / 'tdata/user_data/media_cache').exists())
        self.assertTrue((self.telegram / 'user_data/media_cache/download').exists())

    def test_rsync_exclusions_are_anchored_to_actual_safe_cache_paths(self):
        cache = self.telegram / 'user_data/media_cache'
        cache.mkdir(parents=True)
        with mock.patch.object(state, '_find_cache_dirs', return_value=[str(cache)]), \
             mock.patch.object(backups.sys, 'platform', 'linux'), \
             mock.patch.object(backups.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(backups._copy_tdata_excluding_cache(str(self.telegram), str(self.root / 'copy'))[0])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--exclude') + 1], '/user_data/media_cache/')
        self.assertNotIn('media_cache/', args)

    def test_restore_each_client_leaves_other_profile_untouched(self):
        for root in (self.telegram, self.ayu):
            self.write(root, 'history.db', str(root).encode())
        backup = self.make_backup()
        for client, target, other in [('Telegram', self.telegram, self.ayu), ('AyuGram', self.ayu, self.telegram)]:
            self.write(target, 'history.db', b'changed')
            before = self.digest(other / 'history.db')
            result = backups.restore_backup(str(backup), str(self.account), client)
            self.assertTrue(result[0], result[1])
            original = backup / ('ayugram/tdata' if client == 'AyuGram' else 'tdata') / 'history.db'
            self.assertEqual(self.digest(target / 'history.db'), self.digest(original))
            self.assertEqual(self.digest(other / 'history.db'), before)
            self.assertTrue(list(target.parent.glob('tdata.bak.*')))

    def test_restore_ayu_to_account_without_an_ayu_profile(self):
        self.write(self.ayu, 'ayudata.db', b'saved history')
        backup = self.make_backup()
        destination = self.root / 'FreshAccount'
        telegram = destination / 'TelegramForcePortable/tdata'
        telegram.mkdir(parents=True)
        self.write(telegram, 'key_datas', b'original telegram')
        result = backups.restore_backup(str(backup), str(destination), 'AyuGram')
        self.assertTrue(result[0], result[1])
        self.assertEqual((destination / '.ayugram/TelegramForcePortable/tdata/ayudata.db').read_bytes(),
                         b'saved history')
        self.assertEqual((telegram / 'key_datas').read_bytes(), b'original telegram')

    def test_ayu_restore_rename_failure_recovers_original_history(self):
        self.write(self.ayu, 'ayudata.db', b'original')
        backup = self.make_backup()
        self.write(self.ayu, 'ayudata.db', b'current')
        rename = os.rename
        def fail_install(source, destination):
            if source == str(self.ayu) + '.new':
                raise OSError('injected install failure')
            return rename(source, destination)
        with mock.patch.object(backups.os, 'rename', side_effect=fail_install):
            result = backups.restore_backup(str(backup), str(self.account), 'AyuGram')
        self.assertFalse(result[0])
        self.assertEqual((self.ayu / 'ayudata.db').read_bytes(), b'current')
        self.assertFalse((self.ayu.parent / '.tdata-restore.json').exists())

    def test_ayu_recovery_after_crash_and_marker_restore(self):
        marker = self.account / '.ayugram/.new-login.json'
        marker.write_text('{"new_login": true}')
        self.write(self.ayu, 'ayudata.db', b'original')
        backup = self.make_backup()
        marker.write_text('{"new_login": false}')
        result = backups.restore_backup(str(backup), str(self.account), 'AyuGram')
        self.assertTrue(result[0], result[1])
        self.assertEqual(json.loads(marker.read_text()), {'new_login': True})
        previous = self.ayu.parent / 'tdata.bak.crash'
        self.ayu.rename(previous)
        journal = self.ayu.parent / '.tdata-restore.json'
        journal.write_text(json.dumps({'backup': previous.name}))
        self.assertTrue(backups.recover_interrupted_restore(str(self.account), 'AyuGram'))
        self.assertEqual((self.ayu / 'ayudata.db').read_bytes(), b'original')
        self.assertFalse(journal.exists())

    def test_symlinked_restore_source_never_changes_live_profile(self):
        self.write(self.ayu, 'ayudata.db', b'original')
        backup = self.make_backup()
        os.symlink(self.telegram, backup / 'ayugram/tdata/linked')
        self.assertFalse(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])
        self.assertEqual((self.ayu / 'ayudata.db').read_bytes(), b'original')

    def test_unknown_process_status_refuses_backup_and_restore(self):
        self.write(self.telegram, 'history', b'old')
        backup = self.make_backup()
        with mock.patch.object(server, '_get_ps_output', return_value=None):
            self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
            self.assertFalse(backups.restore_backup(str(backup), str(self.account))[0])
        self.assertEqual((self.telegram / 'history').read_bytes(), b'old')

    def test_ayugram_running_refuses_both_operations(self):
        backup = self.make_backup()
        executable = server.ayugram.executable(str(self.account))
        with mock.patch.object(server, '_get_ps_output', return_value='456 ' + executable):
            self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
            self.assertFalse(backups.restore_backup(str(backup), str(self.account), 'AyuGram')[0])

    def test_canonical_backup_source_with_logical_temp_root_can_restore(self):
        logical_root = self.temp.name
        self.write(self.telegram, 'history', b'original')
        backup = self.make_backup()
        self.write(self.telegram, 'history', b'changed')
        with mock.patch.object(state, 'ROOT_DIR', logical_root), \
             mock.patch.object(state, 'DATA_DIR', logical_root):
            self.assertTrue(backups._safe_tree(str(backup / 'tdata')))
            result = backups.restore_backup(str(backup), os.path.join(logical_root, 'Account'))
            self.assertTrue(result[0], result[1])
            os.symlink(self.telegram, self.ayu / 'linked')
            self.assertFalse(backups._safe_tree(str(self.ayu)))
        self.assertEqual((self.telegram / 'history').read_bytes(), b'original')

    def test_symlink_source_and_backup_root_are_refused(self):
        self.write(self.ayu, 'history')
        os.symlink(self.telegram, self.ayu / 'linked')
        self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
        (self.ayu / 'linked').unlink()
        outside = self.root / 'outside'
        outside.mkdir()
        os.symlink(outside, self.root / 'Backups')
        self.assertIsNone(backups._resolve_backup_dir(str(self.root / 'Backups/date/account')))
        self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])

    def test_ayu_copy_failure_keeps_live_and_completed_backup(self):
        first = self.make_backup()
        copy_profile = backups._copy_tdata_excluding_cache
        def fail_ayu(source, destination, *args, **kwargs):
            if source == str(self.ayu):
                return False, 'fixture copy failure'
            return copy_profile(source, destination, *args, **kwargs)
        with mock.patch.object(backups, '_copy_tdata_excluding_cache', side_effect=fail_ayu):
            self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
        self.assertTrue((first / 'ayugram/tdata').is_dir())
        self.assertTrue(self.ayu.is_dir())
        self.assertEqual(len(backups.list_backups()), 1)

    def test_verification_backup_can_preserve_all_existing_versions(self):
        with mock.patch.dict(state.config, {'backup_keep_per_account': 1}), \
             mock.patch.object(backups, 'prune_backups', self._prune):
            first = self.make_backup()
            result = backups.backup_account(str(self.account), 'Account', prune=False)
        self.assertTrue(result[0], result[1])
        self.assertTrue(first.is_dir())
        self.assertTrue(Path(result[2]).is_dir())
        self.assertEqual(len(backups.list_backups()), 2)

    def test_pruning_keeps_latest_ayu_when_newer_backup_is_telegram_only(self):
        account_id = '12345678-1234-1234-1234-123456789abc'
        paths = []
        for i in range(3):
            backup = self.root / 'Backups' / f'2026-10-{3 - i:02}' / account_id
            self.write(backup / 'tdata', 'key_datas', b'fixture key')
            if i:
                self.write(backup / 'ayugram/tdata', 'key_datas', b'fixture ayu key')
            self.write(backup, 'backup.json', json.dumps({'account_id': account_id,
                       'account_name': 'Account', 'created_at': str(i)}).encode())
            paths.append(backup)
        with mock.patch.dict(state.config, {'backup_keep_per_account': 1}):
            # The setUp patch prevents pruning during backup creation; exercise original here.
            original = self._prune
            original(account_id)
        self.assertTrue(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertFalse(paths[2].exists())

    _prune = staticmethod(backups.prune_backups)
