import copy
import json
import os
import plistlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state
import backups


class AuditFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        for module in (state, server):
            for key in ('ROOT_DIR', 'DATA_DIR'):
                patch = mock.patch.object(module, key, self.root)
                patch.start()
                self.addCleanup(patch.stop)
        for key in ('METADATA_FILE', 'WORKSPACES_FILE'):
            patch = mock.patch.object(state, key, os.path.join(self.root, key + '.json'))
            patch.start()
            self.addCleanup(patch.stop)
        self.meta = copy.deepcopy(state.metadata)
        self.addCleanup(self.restore_metadata)

    def restore_metadata(self):
        state.metadata.clear()
        state.metadata.update(self.meta)

    def account(self, name='account'):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.join(path, 'TelegramForcePortable', 'tdata'))
        return path

    def bundle(self, parent, version):
        app = os.path.join(parent, 'Telegram.app')
        os.makedirs(os.path.join(app, 'Contents', 'MacOS'))
        os.makedirs(os.path.join(app, 'Contents', '_CodeSignature'))
        with open(os.path.join(app, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleIdentifier': server.TDESKTOP_BUNDLE_ID,
                          'CFBundleShortVersionString': version}, f)
        Path(app, 'Contents', 'MacOS', 'Telegram').write_bytes(b'executable')
        Path(app, 'Contents', '_CodeSignature', 'CodeResources').write_bytes(b'seal')
        return app

    def test_missing_source_leaves_no_folder_and_retry_can_succeed(self):
        with mock.patch.dict(server.config, {'app_source': ''}), \
             mock.patch.object(server, 'get_shared_app', return_value=None), \
             mock.patch.object(server, 'scan_accounts_cached', return_value=[]):
            ok, _ = server.create_account('New', self.root, False)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'New')))
        source = self.bundle(os.path.join(self.root, 'source'), '7.3.1')
        with mock.patch.object(server, 'get_shared_app', return_value=source), \
             mock.patch.object(server, '_patch_app_display_name', return_value=True):
            ok, _ = server.create_account('New', self.root, False)
        self.assertTrue(ok)

    def test_relocation_preserves_concurrent_note_and_workspace_save(self):
        old = self.account('Old')
        new = os.path.join(self.root, 'New')
        state.metadata.clear()
        state.metadata.update({'notes': {old: 'original'}})
        ws = {'old': {'accounts': [old]}}
        attempted = threading.Event()
        finished = threading.Event()
        writer = None
        rename = os.rename

        def edit():
            attempted.set()
            with state._meta_lock, state._ws_lock:
                state.metadata['notes']['other'] = 'saved concurrently'
                ws['other'] = {'accounts': ['other']}
            finished.set()

        def rename_with_edit(*args):
            nonlocal writer
            writer = threading.Thread(target=edit)
            writer.start()
            self.assertTrue(attempted.wait(2))
            finished.wait(0.1)
            return rename(*args)

        def save_ws(value):
            ws.clear()
            ws.update(copy.deepcopy(value))

        with mock.patch.object(server.os, 'rename', side_effect=rename_with_edit), \
             mock.patch.object(server, 'load_workspaces', side_effect=lambda: copy.deepcopy(ws)), \
             mock.patch.object(server, 'save_workspaces', side_effect=save_ws), \
             mock.patch.object(server, 'save_metadata'), \
             mock.patch.object(server, 'set_telegram_display_name'):
            ok, _ = server._relocate_account(old, new)
            writer.join(2)
        self.assertTrue(ok)
        self.assertFalse(writer.is_alive())
        self.assertEqual(state.metadata['notes'].get('other'), 'saved concurrently')
        self.assertEqual(state.metadata['notes'][new], 'original')
        self.assertEqual(ws['old']['accounts'], [new])
        self.assertIn('other', ws)

    def test_scan_recovers_original_after_process_dies_between_restore_renames(self):
        account = self.account()
        tdata = Path(account, 'TelegramForcePortable', 'tdata')
        (tdata / 'original').write_text('session')
        backup = Path(self.root, 'Backups', 'date', 'account')
        (backup / 'tdata').mkdir(parents=True)
        (backup / 'tdata' / 'replacement').write_text('replacement session')
        (backup / 'tdata' / 'key_datas').write_bytes(b'session-key')
        rename = os.rename

        def crash(src, dst):
            if str(src).endswith('tdata.new'):
                raise KeyboardInterrupt('simulated process death')
            return rename(src, dst)

        with mock.patch.object(backups.os, 'rename', side_effect=crash), \
             mock.patch.object(server, 'find_telegram_pid', return_value=None):
            with self.assertRaises(KeyboardInterrupt):
                backups.restore_backup(str(backup), account)
        self.assertFalse(tdata.exists())
        with mock.patch.object(server, 'get_shared_app', return_value=None), \
             mock.patch.object(server, '_get_ps_output', return_value=''):
            found = server.scan_accounts()
        self.assertTrue((tdata / 'original').is_file())
        self.assertTrue(any(a['path'] == account and a['has_tdata'] for a in found))

    def test_open_refuses_downgrade_before_deleting_updated_clone(self):
        account = self.account()
        app = self.bundle(account, '7.3.0')
        shared = self.bundle(os.path.join(self.root, 'master'), '7.2.8')
        with mock.patch.object(server, 'get_shared_app', return_value=shared), \
             mock.patch.object(server, '_get_ps_output', return_value=''), \
             mock.patch.object(server, 'find_client_pid', return_value=None), \
             mock.patch.object(server.subprocess, 'run') as run:
            ok, message = server.open_account(account)
        self.assertFalse(ok)
        self.assertIn('7.3', message)
        self.assertTrue(os.path.isdir(app))
        run.assert_not_called()

    def test_open_refuses_downgrade_after_watcher_already_removed_clone(self):
        account = self.account()
        Path(account, 'TelegramForcePortable', 'log.txt').write_text(
            '[2026.10.11 04:41:55] Launched version: 7003000, install beta: [FALSE]\n')
        shared = self.bundle(os.path.join(self.root, 'master'), '7.2.8')
        with mock.patch.object(server, 'get_shared_app', return_value=shared), \
             mock.patch.object(server, '_get_ps_output', return_value=''), \
             mock.patch.object(server, 'find_client_pid', return_value=None), \
             mock.patch.object(server.subprocess, 'run') as run:
            ok, message = server.open_account(account)
        self.assertFalse(ok)
        self.assertIn('7.3', message)
        run.assert_not_called()

    def test_idle_cleanup_preserves_a_clone_newer_than_master(self):
        account = self.account()
        app = self.bundle(account, '7.3.0')
        shared = self.bundle(os.path.join(self.root, 'master'), '7.2.8')
        with mock.patch.object(server, 'get_shared_app', return_value=shared), \
             mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            removed = server._remove_clone_if_idle(account)
        self.assertFalse(removed)
        self.assertTrue(os.path.isdir(app))

    def test_completed_restore_keeps_previous_session_and_clears_journal(self):
        account = self.account()
        portable = Path(account, 'TelegramForcePortable')
        (portable / 'tdata' / 'original').write_text('original')
        backup = Path(self.root, 'Backups', 'date', 'account')
        (backup / 'tdata').mkdir(parents=True)
        (backup / 'tdata' / 'replacement').write_text('replacement')
        (backup / 'tdata' / 'key_datas').write_bytes(b'session-key')
        with mock.patch.object(server, 'find_telegram_pid', return_value=None):
            ok, _ = backups.restore_backup(str(backup), account)
        self.assertTrue(ok)
        self.assertTrue((portable / 'tdata' / 'replacement').exists())
        self.assertEqual(len(list(portable.glob('tdata.bak.*/original'))), 1)
        self.assertFalse((portable / '.tdata-restore.json').exists())

    def test_recovery_keeps_committed_replacement_after_crash_before_journal_cleanup(self):
        account = self.account()
        portable = Path(account, 'TelegramForcePortable')
        (portable / 'tdata' / 'replacement').write_text('replacement')
        (portable / 'tdata.bak.original').mkdir()
        (portable / 'tdata.bak.original' / 'original').write_text('original')
        marker = portable / '.tdata-restore.json'
        marker.write_text(json.dumps({'backup': 'tdata.bak.original'}))
        self.assertTrue(backups.recover_interrupted_restore(account))
        self.assertTrue((portable / 'tdata' / 'replacement').exists())
        self.assertTrue((portable / 'tdata.bak.original' / 'original').exists())
        self.assertFalse(marker.exists())

    def test_invalid_restore_journal_blocks_open_without_writing_session(self):
        account = self.account()
        portable = Path(account, 'TelegramForcePortable')
        (portable / 'tdata').rmdir()
        marker = portable / '.tdata-restore.json'
        marker.write_text(json.dumps({'backup': 'tdata.bak.original/../../outside'}))
        with mock.patch.object(server.subprocess, 'run') as run:
            ok, _ = server.open_account(account)
        self.assertFalse(ok)
        self.assertFalse((portable / 'tdata').exists())
        self.assertTrue(marker.exists())
        run.assert_not_called()

    def test_recovery_refuses_symlink_to_original_session(self):
        account = self.account()
        portable = Path(account, 'TelegramForcePortable')
        (portable / 'tdata').rmdir()
        original = Path(self.root, 'outside')
        original.mkdir()
        (portable / 'tdata.bak.original').symlink_to(original, target_is_directory=True)
        (portable / '.tdata-restore.json').write_text(json.dumps({'backup': 'tdata.bak.original'}))
        self.assertFalse(backups.recover_interrupted_restore(account))
        self.assertTrue(original.exists())
        self.assertFalse((portable / 'tdata').exists())

    def test_staged_newer_update_is_preserved_until_shared_app_catches_up(self):
        account = self.account()
        app = self.bundle(account, '7.2.8')
        shared = self.bundle(os.path.join(self.root, 'master'), '7.2.8')
        staged = Path(account, 'TelegramForcePortable', 'tupdates', 'temp')
        (staged / 'tdata').mkdir(parents=True)
        (staged / 'ready').write_bytes(b'1')
        (staged / 'tdata' / 'version').write_bytes(b'\x00' * 8 + '7.3.0'.encode('utf-32-le'))
        with mock.patch.object(server, 'get_shared_app', return_value=shared), \
             mock.patch.object(server, '_get_ps_output', return_value=''), \
             mock.patch.object(server, 'find_client_pid', return_value=None), \
             mock.patch.object(server.subprocess, 'run') as run:
            ok, message = server.open_account(account)
        self.assertFalse(ok)
        self.assertIn('7.3.0', message)
        self.assertTrue(os.path.isdir(app))
        self.assertTrue((staged / 'ready').exists())
        run.assert_not_called()

    def test_matching_shared_version_allows_open_after_update(self):
        account = self.account()
        app = self.bundle(account, '7.3.0')
        Path(account, 'TelegramForcePortable', 'log.txt').write_text('Launched version: 7003000\n')
        shared = self.bundle(os.path.join(self.root, 'master'), '7.3.0')
        server._write_clone_stamp(account, server._bundle_fingerprint(shared))
        with mock.patch.object(server, 'get_shared_app', return_value=shared), \
             mock.patch.object(server, '_get_ps_output', return_value=''), \
             mock.patch.object(server, 'find_client_pid', side_effect=[None, 123]), \
             mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=0, stderr=b'')) as run:
            ok, _ = server.open_account(account)
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0], ['open', '-n', app, '--args', '-noupdate'])
