import os
import plistlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import ayugram
import server
import backups
import keeper
import state


class LifecycleSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = self.root / 'account'
        self.tdata = self.account / 'TelegramForcePortable/tdata'
        self.tdata.mkdir(parents=True)
        self.key = self.tdata / 'key_datas'
        self.key.write_bytes(b'TDF$' + struct.pack('<i', 7001003) + b'session')
        self.master = self.bundle(self.root / 'master/Telegram.app')
        self.app = self.bundle(self.account / 'Telegram.app')
        for module in (state, server):
            for name in ('ROOT_DIR', 'DATA_DIR'):
                patch = mock.patch.object(module, name, str(self.root))
                patch.start()
                self.addCleanup(patch.stop)
        self.addCleanup(server._clear_watcher_exempt, str(self.account))
        server._write_clone_stamp(str(self.account), server._bundle_fingerprint(str(self.master)))

    def bundle(self, app, client='Telegram'):
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/_CodeSignature').mkdir()
        (app / 'Contents/MacOS' / client).write_bytes(b'executable')
        (app / 'Contents/_CodeSignature/CodeResources').write_bytes(b'seal')
        with (app / 'Contents/Info.plist').open('wb') as f:
            plistlib.dump({'CFBundleIdentifier': ayugram.BUNDLE_ID if client == 'AyuGram' else server.TDESKTOP_BUNDLE_ID,
                          'CFBundleShortVersionString': '7.1.3'}, f)
        return app

    def open(self, shared=True, unknown=False):
        original_run = server.subprocess.run
        launched = False
        def run(command, **kwargs):
            nonlocal launched
            if command[0] == 'open':
                launched = True
                return mock.Mock(returncode=0, stderr=b'')
            if command[0] in ('xattr', 'codesign'):
                return mock.Mock(returncode=0)
            return original_run(command, **kwargs)
        def processes(**kwargs):
            if unknown and not launched:
                return None
            app = server.find_account_app(str(self.account))
            return f'123 {app}/Contents/MacOS/Telegram' if launched else '1 /bin/sleep'
        with mock.patch.object(server, 'get_shared_app', return_value=str(self.master) if shared else None), \
             mock.patch.object(server, '_get_ps_output', side_effect=processes), \
             mock.patch.object(server.subprocess, 'run', side_effect=run), \
             mock.patch.object(server, 'save_metadata'):
            return server.open_account(str(self.account)), launched

    def test_unknown_process_state_refuses_telegram_open(self):
        result, launched = self.open(unknown=True)
        self.assertFalse(result[0])
        self.assertFalse(launched)

    def test_header_blocks_downgrade_with_or_without_shared_master(self):
        self.key.write_bytes(b'TDF$' + struct.pack('<i', 7003000) + b'session')
        for shared in (True, False):
            result, launched = self.open(shared=shared)
            self.assertFalse(result[0])
            self.assertFalse(launched)
        self.assertTrue(self.app.exists())

    def test_actual_clone_integrity_replaces_modified_executable(self):
        executable = self.app / 'Contents/MacOS/Telegram'
        executable.write_bytes(b'corrupt')
        result, launched = self.open()
        self.assertTrue(result[0], result)
        self.assertTrue(launched)
        self.assertEqual((Path(server.find_account_app(str(self.account))) / 'Contents/MacOS/Telegram').read_bytes(), b'executable')
        self.assertEqual(self.key.read_bytes()[-7:], b'session')

    def test_actual_clone_integrity_replaces_modified_library(self):
        (self.master / 'Contents/library.dylib').write_bytes(b'official')
        (self.app / 'Contents/library.dylib').write_bytes(b'official')
        server._write_clone_stamp(str(self.account), server._bundle_fingerprint(str(self.master)))
        (self.app / 'Contents/library.dylib').write_bytes(b'corrupt')
        result, _ = self.open()
        self.assertTrue(result[0], result)
        self.assertEqual((Path(server.find_account_app(str(self.account))) / 'Contents/library.dylib').read_bytes(), b'official')

    def test_all_telegram_bundles_are_checked_and_ambiguous_open_refused(self):
        other = self.bundle(self.account / 'Other.app')
        output = f'789 {other}/Contents/MacOS/Telegram'
        self.assertEqual(server._client_pid_from_ps_output(str(self.account), output, 'Telegram'), 789)
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertFalse(server.open_account(str(self.account))[0])
        run.assert_not_called()

    def test_missing_bundles_do_not_hide_exact_default_or_custom_processes(self):
        self.app.rename(self.root / 'moved-Telegram.app')
        for client, relative in (
                ('Telegram', 'Telegram.app/Contents/MacOS/Telegram'),
                ('Telegram', 'Custom Telegram.app/Contents/MacOS/Telegram'),
                ('AyuGram', '.ayugram/AyuGram.app/Contents/MacOS/AyuGram'),
                ('AyuGram', '.ayugram/Custom Ayu.app/Contents/MacOS/AyuGram')):
            with self.subTest(client=client, relative=relative), \
                 mock.patch.object(server, '_get_ps_output', return_value=f'789 {self.account / relative} -noupdate'):
                self.assertEqual(server.find_client_pid(str(self.account), client, fresh=True), 789)
                self.assertEqual(server.find_telegram_pid(str(self.account), fresh=True), 789)
                self.assertTrue(server._pid_matches_account(789, str(self.account), client))

    def test_missing_bundle_process_blocks_backup_restore_cache_and_second_launch(self):
        self.app.rename(self.root / 'moved-Telegram.app')
        backup = self.root / 'Backups/date/account'
        (backup / 'tdata').mkdir(parents=True)
        (backup / 'tdata/key_datas').write_bytes(b'fixture backup key')
        cache = self.tdata / 'user_data/media_cache/local'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'keep cache')
        original_key = self.key.read_bytes()
        for client, relative in (
                ('Telegram', 'Missing Telegram.app/Contents/MacOS/Telegram'),
                ('AyuGram', '.ayugram/Missing Ayu.app/Contents/MacOS/AyuGram')):
            with self.subTest(client=client), \
                 mock.patch.object(server, '_get_ps_output', return_value=f'789 {self.account / relative}'), \
                 mock.patch.object(server, 'scan_accounts', return_value=[]), \
                 mock.patch.object(state, 'ensure_account_id', return_value='11111111-1111-4a11-8b11-111111111111'):
                self.assertFalse(backups.backup_account(str(self.account), 'Account')[0])
                self.assertFalse(backups.restore_backup(str(backup), str(self.account))[0])
                self.assertEqual(server.clear_account_caches(str(self.account)), (False, 0))
                with mock.patch.object(server.subprocess, 'run') as run:
                    if client == 'Telegram':
                        self.assertEqual(server.open_account(str(self.account)), (True, 'already running'))
                    else:
                        self.assertFalse(ayugram.open_account(str(self.account))[0])
                    run.assert_not_called()
                self.assertEqual(self.key.read_bytes(), original_key)
                self.assertEqual(cache.read_bytes(), b'keep cache')

    def test_missing_bundle_match_rejects_shell_text_other_accounts_and_nested_apps(self):
        self.app.rename(self.root / 'moved-Telegram.app')
        executable = f'{self.account}/Missing.app/Contents/MacOS/Telegram'
        false_commands = (
            '/bin/zsh -c echo ' + executable,
            f'{self.account}-other/Missing.app/Contents/MacOS/Telegram',
            f'{self.account}/nested/Missing.app/Contents/MacOS/Telegram',
            executable + 'Helper',
            executable + '/nested',
            f'{self.account}/Missing.app/Contents/MacOS/AyuGram',
        )
        for command in false_commands:
            with self.subTest(command=command):
                self.assertIsNone(server._client_pid_from_ps_output(str(self.account), '789 ' + command, 'Telegram'))

    def test_clone_custom_name_survives_recreation_without_repeated_signing(self):
        with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): 'Custom Name'}}):
            with mock.patch.object(server, 'get_shared_app', return_value=str(self.master)), \
                 mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
                self.assertTrue(server._remove_clone_if_idle(str(self.account)))
            result, _ = self.open()
            self.assertTrue(result[0], result)
            named = self.account / 'Custom Name.app'
            self.assertTrue(named.exists())
            with (named / 'Contents/Info.plist').open('rb') as f:
                self.assertEqual(plistlib.load(f)['CFBundleDisplayName'], 'Custom Name')
            before = server._bundle_fingerprint(str(named))
            result, _ = self.open()
            self.assertTrue(result[0], result)
            self.assertEqual(server._bundle_fingerprint(str(named)), before)

    def test_keeper_never_closes_a_reopened_pid(self):
        def launch(path, launched_process=None):
            launched_process.update(pid=123, executable=str(self.app / 'Contents/MacOS/Telegram'))
            return True, ''
        with mock.patch.object(server, 'scan_accounts', return_value=[{'path': str(self.account), 'name': 'account', 'status': 'ready', 'running': False}]), \
             mock.patch.dict(state.metadata, {'last_opened': {}}), \
             mock.patch.object(server, 'open_account', side_effect=launch), \
             mock.patch.object(server, '_get_ps_output', return_value=f'456 {self.app}/Contents/MacOS/Telegram'), \
             mock.patch.object(keeper.time, 'sleep'), \
             mock.patch.object(server, '_request_client_quit') as quit_request:
            keeper._run_keeper_pass(30, 0)
        quit_request.assert_not_called()

    def test_ayugram_idle_cleanup_removes_only_verified_clone(self):
        master = self.bundle(self.root / '_apps/macOS/AyuGram.app', 'AyuGram')
        runtime = self.account / '.ayugram'
        app = self.bundle(runtime / 'AyuGram.app', 'AyuGram')
        tdata = runtime / 'TelegramForcePortable/tdata'
        tdata.mkdir(parents=True)
        history = tdata / 'ayudata.db'
        history.write_bytes(b'history')
        state._save_json_atomic(str(master.parent / '.ayugram-official.json'), {'fingerprint': ayugram.bundle_fingerprint(str(master))})
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            self.assertTrue(ayugram.remove_clone_if_idle(str(self.account)))
        self.assertFalse(app.exists())
        self.assertEqual(history.read_bytes(), b'history')

    def test_ayugram_ambiguous_clone_process_is_still_detected(self):
        first = self.bundle(self.account / '.ayugram/First.app', 'AyuGram')
        self.bundle(self.account / '.ayugram/Second.app', 'AyuGram')
        with mock.patch.object(server, '_get_ps_output', return_value=f'321 {first}/Contents/MacOS/AyuGram'):
            self.assertEqual(server.find_telegram_pid(str(self.account), fresh=True), 321)
            self.assertTrue(server._pid_matches_account(321, str(self.account), 'AyuGram'))

    def test_ayugram_cleanup_refuses_running_unknown_and_untrusted_master(self):
        master = self.bundle(self.root / '_apps/macOS/AyuGram.app', 'AyuGram')
        app = self.bundle(self.account / '.ayugram/AyuGram.app', 'AyuGram')
        state._save_json_atomic(str(master.parent / '.ayugram-official.json'), {'fingerprint': ayugram.bundle_fingerprint(str(master))})
        for output in (None, f'321 {app}/Contents/MacOS/AyuGram'):
            with mock.patch.object(server, '_get_ps_output', return_value=output):
                self.assertFalse(ayugram.remove_clone_if_idle(str(self.account)))
            self.assertTrue(app.exists())
        (master / 'Contents/MacOS/AyuGram').write_bytes(b'changed')
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            self.assertFalse(ayugram.remove_clone_if_idle(str(self.account)))
        self.assertTrue(app.exists())

    def test_dock_name_does_not_bless_corrupted_clone_or_edit_running_app(self):
        self.app.joinpath('Contents/MacOS/Telegram').write_bytes(b'changed')
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch.object(server, '_patch_app_display_name') as patch_name:
            self.assertFalse(server.set_telegram_display_name(str(self.account), 'Custom')[0])
        patch_name.assert_not_called()
        self.app.joinpath('Contents/MacOS/Telegram').write_bytes(b'executable')
        with mock.patch.object(server, '_get_ps_output', return_value=f'123 {self.app}/Contents/MacOS/Telegram'), \
             mock.patch.object(server, '_patch_app_display_name') as patch_name:
            self.assertFalse(server.set_telegram_display_name(str(self.account), 'Custom')[0])
        patch_name.assert_not_called()

    def test_nonzero_process_command_is_unknown(self):
        with mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='123 /bin/sleep')):
            self.assertIsNone(server._get_ps_output(force=True, allow_stale=False))

    def test_telegram_symlinked_executable_refuses_launch(self):
        exe = self.app / 'Contents/MacOS/Telegram'
        exe.unlink()
        exe.symlink_to(self.master / 'Contents/MacOS/Telegram')
        result, launched = self.open()
        self.assertFalse(result[0])
        self.assertFalse(launched)

    def test_quit_uses_executable_of_the_selected_alternate_clone(self):
        other = self.bundle(self.account / 'Other.app')
        exe = str(other / 'Contents/MacOS/Telegram')
        with mock.patch.object(server, '_get_ps_output', return_value=f'456 {exe}'), \
             mock.patch.object(server.os.path, 'isfile', return_value=True), \
             mock.patch.object(server.os.path, 'getmtime', return_value=1), \
             mock.patch.object(server.subprocess, 'run') as run:
            server._request_client_quit(456, str(self.account), 'Telegram')
        self.assertEqual(run.call_args.args[0][1:], ['--quit-pid', '456', exe])

    def test_ayugram_close_cleanup_runs_while_telegram_remains_open(self):
        class InlineThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        server._watcher_exempt(str(self.account))
        with mock.patch.object(server, 'find_client_pid', return_value=321), \
             mock.patch.object(server, '_pid_matches_account', side_effect=[True, False]), \
             mock.patch.object(server, '_request_client_quit'), \
             mock.patch.object(server, 'find_telegram_pid', return_value=456), \
             mock.patch.object(server.threading, 'Thread', InlineThread), \
             mock.patch.object(ayugram, 'remove_clone_if_idle', return_value=True) as cleanup:
            self.assertTrue(server.kill_account(str(self.account), client='AyuGram'))
        cleanup.assert_called_once_with(str(self.account))


if __name__ == '__main__':
    unittest.main()
