import hashlib
import json
import os
import plistlib
import sys
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'))
os.environ.setdefault('TG_SESSION_TOKEN', 'unit-test-token')
import server
import state
import ayugram


class AyuGramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for module in (state, server):
            for key in ('ROOT_DIR', 'DATA_DIR'):
                patch = mock.patch.object(module, key, str(root))
                patch.start()
                self.addCleanup(patch.stop)
        self.account = root / 'account'
        self.original = self.account / 'TelegramForcePortable' / 'tdata'
        self.original.mkdir(parents=True)
        (self.original / 'key_datas').write_bytes(b'TDF$' + struct.pack('<i', 7002009) + b'original encrypted session')
        self.master = root / '_apps' / 'macOS' / 'AyuGram.app'
        (self.master / 'Contents' / 'MacOS').mkdir(parents=True)
        (self.master / 'Contents' / '_CodeSignature').mkdir()
        (self.master / 'Contents' / 'MacOS' / 'AyuGram').write_bytes(b'test executable')
        (self.master / 'Contents' / '_CodeSignature' / 'CodeResources').write_bytes(b'test seal')
        with (self.master / 'Contents' / 'Info.plist').open('wb') as f:
            plistlib.dump({'CFBundleIdentifier': 'one.ayugram.AyuGramDesktop',
                          'CFBundleShortVersionString': '7.2.9'}, f)
        state._save_json_atomic(str(self.master.parent / '.ayugram-official.json'),
                                {'fingerprint': ayugram.bundle_fingerprint(str(self.master))})
        self.runtime = self.account / '.ayugram'
        self.copy = self.runtime / 'TelegramForcePortable' / 'tdata'

    @property
    def exe(self):
        return ayugram.executable(str(self.account))

    def open(self, new_login=False, on_launch=None, on_bootstrap=None):
        real_run = server.subprocess.run
        launched = False
        def run(command, *args, **kwargs):
            nonlocal launched
            if command[0] in ('codesign', 'xattr'):
                return mock.Mock(returncode=0, stderr=b'')
            if command[0] == 'open':
                if '-j' in command:
                    if on_bootstrap:
                        on_bootstrap(command)
                    (self.copy / 'settingss').write_bytes(b'TDF$' + struct.pack('<i', 7002009) + b'initialized core settings')
                elif on_launch:
                    on_launch(command)
                launched = True
                return mock.Mock(returncode=0, stderr=b'')
            return real_run(command, *args, **kwargs)
        def quit_bootstrap(*args):
            nonlocal launched
            launched = False
        with mock.patch.object(server.subprocess, 'run', side_effect=run), \
             mock.patch.object(server, '_request_client_quit', side_effect=quit_bootstrap), \
             mock.patch.object(server, '_get_ps_output', side_effect=lambda **kwargs: '321 '+self.exe if launched else '1 /bin/sleep'):
            return ayugram.open_account(str(self.account), new_login=new_login)

    def test_fresh_login_bootstraps_native_settings_before_visible_launch(self):
        visible = []
        def launched(command):
            visible.append(command)
            self.assertTrue((self.copy / 'settingss').exists())
            self.assertIn('-ghost', command)
        self.assertTrue(self.open(new_login=True, on_launch=launched)[0])
        self.assertEqual(len(visible), 1)
        self.assertTrue(self.open(on_launch=launched)[0])
        self.assertEqual(len(visible), 2)

    def test_failed_native_initialization_never_launches_visible_login(self):
        with mock.patch.object(ayugram, '_initialize_native_settings', return_value=(False, 'Initialization failed')):
            visible = []
            ok, message = self.open(new_login=True, on_launch=lambda command: visible.append(command))
        self.assertFalse(ok)
        self.assertIn('Initialization failed', message)
        self.assertFalse(visible)
        self.assertFalse(any(self.copy.glob('key_data*')))
        self.assertTrue((self.runtime / '.new-login.json').exists())

    def test_interrupted_native_initialization_completes_saved_intent_on_retry(self):
        self.copy.mkdir(parents=True)
        state._save_json_atomic(str(self.runtime / '.new-login.json'), {'new_login': True})
        ayugram._apply_common_profile(str(self.account))
        path = self.copy / 'ayu_settings.json'
        intended = json.loads(path.read_text())
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            self.assertFalse(ayugram._initialize_native_settings(str(self.account), str(self.master))[0])
        marker = self.runtime / '.common-profile.json'
        self.assertEqual(json.loads(marker.read_text())['native_initialization']['preferences'], intended)
        (self.copy / 'settingss').write_bytes(b'TDF$' + struct.pack('<i', 7002009) + b'initialized core settings')
        path.write_text(json.dumps({'useGlobalGhostMode': True, 'ghostModeSettings': {'0': {'sendReadMessages': True}}}))
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertTrue(ayugram._initialize_native_settings(str(self.account), str(self.master))[0])
        run.assert_not_called()
        self.assertEqual(json.loads(path.read_text()), intended)
        self.assertNotIn('native_initialization', json.loads(marker.read_text()))

    def test_initialization_that_does_not_quit_keeps_pending_intent_without_force(self):
        self.copy.mkdir(parents=True)
        state._save_json_atomic(str(self.runtime / '.new-login.json'), {'new_login': True})
        ayugram._apply_common_profile(str(self.account))
        with mock.patch.object(server, '_get_ps_output', side_effect=['1 /bin/sleep'] + [f'321 {self.exe}'] * 21), \
             mock.patch.object(server, '_request_client_quit') as quit_request, \
             mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
             mock.patch.object(ayugram.time, 'sleep'):
            ok, message = ayugram._initialize_native_settings(str(self.account), str(self.master))
        self.assertFalse(ok)
        self.assertIn('quit normally', message)
        quit_request.assert_called_once_with(321, str(self.account), 'AyuGram')
        self.assertEqual(len(run.call_args_list), 1)
        self.assertTrue(json.loads((self.runtime / '.common-profile.json').read_text())['native_initialization'])

    def test_truncated_native_settings_leave_initialization_pending(self):
        self.copy.mkdir(parents=True)
        state._save_json_atomic(str(self.runtime / '.new-login.json'), {'new_login': True})
        ayugram._apply_common_profile(str(self.account))
        marker = self.runtime / '.common-profile.json'
        applied = json.loads(marker.read_text())
        applied['native_initialization'] = {'preferences': json.loads((self.copy / 'ayu_settings.json').read_text())}
        marker.write_text(json.dumps(applied))
        (self.copy / 'settingss').write_bytes(b'TDF$')
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertFalse(ayugram._initialize_native_settings(str(self.account), str(self.master))[0])
        run.assert_not_called()
        self.assertIn('native_initialization', json.loads(marker.read_text()))

    def test_new_login_ignores_newer_original_and_reopens_without_import(self):
        original = b'TDF$' + struct.pack('<i', 7003001) + b'original'
        (self.original / 'key_datas').write_bytes(original)
        with mock.patch('backups._copy_tdata_excluding_cache') as copy:
            self.assertTrue(self.open(new_login=True)[0])
            self.assertTrue(self.open()[0])
            copy.assert_not_called()
        self.assertFalse((self.copy / 'key_datas').exists())
        settings = json.loads((self.copy / 'ayu_settings.json').read_text())
        self.assertTrue(settings['saveDeletedMessages'])
        self.assertTrue(settings['saveMessagesHistory'])
        self.assertEqual((self.original / 'key_datas').read_bytes(), original)

    def test_new_login_never_resets_existing_imported_profile(self):
        self.assertTrue(self.open()[0])
        before = (self.copy / 'key_datas').read_bytes()
        self.assertFalse(self.open(new_login=True)[0])
        self.assertEqual((self.copy / 'key_datas').read_bytes(), before)

    def test_new_login_can_launch_while_original_telegram_is_running(self):
        real_find = server.find_client_pid
        def find(path, client, fresh=False):
            return 456 if client == 'Telegram' else real_find(path, client, fresh=fresh)
        with mock.patch.object(server, 'find_client_pid', side_effect=find), \
             mock.patch('backups._copy_tdata_excluding_cache') as copy:
            self.assertTrue(self.open(new_login=True)[0])
            copy.assert_not_called()

    def test_first_import_is_refused_while_original_telegram_is_running(self):
        with mock.patch.object(server, 'find_client_pid', side_effect=lambda path, client, **kw: 456 if client == 'Telegram' else None):
            self.assertFalse(self.open()[0])
        self.assertFalse(self.copy.exists())

    def test_close_signals_only_selected_client_when_both_are_running(self):
        telegram = self.account / 'Telegram.app'
        (telegram / 'Contents' / 'MacOS').mkdir(parents=True)
        (telegram / 'Contents' / 'MacOS' / 'Telegram').write_bytes(b'executable')
        output = f'456 {telegram}/Contents/MacOS/Telegram\n321 {self.exe}'
        with mock.patch.object(server, '_get_ps_output', return_value=output), \
             mock.patch.object(server, 'find_account_app', return_value=str(telegram)), \
             mock.patch.object(server, '_request_client_quit') as quit_request, \
             mock.patch.object(server.subprocess, 'run') as run, \
             mock.patch.object(server.threading, 'Thread'):
            self.assertTrue(server.kill_account(str(self.account), client='AyuGram'))
            quit_request.assert_called_with(321, str(self.account), 'AyuGram')
            run.assert_not_called()
            self.assertTrue(server.kill_account(str(self.account), client='Telegram'))
            quit_request.assert_called_with(456, str(self.account), 'Telegram')
            run.assert_not_called()
            self.assertFalse(server._pid_matches_account(321, str(self.account), 'Telegram'))
            self.assertFalse(server._pid_matches_account(456, str(self.account), 'AyuGram'))

    def test_native_quit_targets_pid_and_exact_executable(self):
        with mock.patch.object(server, '_get_ps_output', return_value=f'321 {self.exe}'), \
             mock.patch.object(server.os.path, 'isfile', return_value=True), \
             mock.patch.object(server.os.path, 'getmtime', return_value=1), \
             mock.patch.object(server.subprocess, 'run') as run:
            server._request_client_quit(321, str(self.account), 'AyuGram')
        self.assertEqual(run.call_args.args[0][1:], ['--quit-pid', '321', self.exe])

    def test_telegram_update_control_is_outside_session_and_tracks_renamed_app(self):
        before = {p.name:p.read_bytes() for p in self.original.iterdir()}
        app = self.account / 'Custom Telegram.app'
        app.mkdir()
        self.assertTrue(server._disable_telegram_auto_updates(str(self.account), str(app))[0])
        marker = self.account / 'externalupdater.d' / 'telegram-manager'
        self.assertEqual(marker.read_text().splitlines(), list(dict.fromkeys((str(app), os.path.realpath(app)))))
        self.assertTrue(server._disable_telegram_auto_updates(str(self.account), str(app))[0])
        renamed = self.account / 'Renamed Telegram.app'
        app.rename(renamed)
        self.assertTrue(server._disable_telegram_auto_updates(str(self.account), str(renamed))[0])
        self.assertEqual(marker.read_text().splitlines(), list(dict.fromkeys((str(renamed), os.path.realpath(renamed)))))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.original.iterdir()})

    def test_telegram_update_control_rejects_symlink_outside_account(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.account / 'externalupdater.d').symlink_to(outside, target_is_directory=True)
        self.assertFalse(server._disable_telegram_auto_updates(str(self.account), str(self.account/'Telegram.app'))[0])
        self.assertEqual(list(outside.iterdir()), [])

    def test_ayugram_update_control_is_separate_and_reused(self):
        original = {p.name:p.read_bytes() for p in self.original.iterdir()}
        self.assertTrue(self.open()[0])
        marker = self.runtime / 'externalupdater.d' / 'telegram-manager'
        app = Path(ayugram.find_app(str(self.account)))
        self.assertIn(str(app), marker.read_text().splitlines())
        before = marker.read_bytes()
        self.assertTrue(self.open()[0])
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(original, {p.name:p.read_bytes() for p in self.original.iterdir()})
        self.assertFalse((self.original / 'externalupdater.d').exists())

    def test_ayugram_refuses_launch_if_update_control_path_is_unsafe(self):
        self.assertTrue(self.open()[0])
        directory = self.runtime / 'externalupdater.d'
        (directory / 'telegram-manager').unlink()
        directory.rmdir()
        directory.symlink_to(self.original, target_is_directory=True)
        before = {p.name:p.read_bytes() for p in self.original.iterdir()}
        ok, message = self.open()
        self.assertFalse(ok)
        self.assertIn('AyuGram updater', message)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.original.iterdir()})

    def test_telegram_upgrade_check_warns_without_changing_any_files(self):
        before = {str(p): p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        def version(app):
            return '7.2.9' if app == str(self.master) else '7.3.1'
        with mock.patch.object(server, 'is_allowed_app_source', return_value=True), \
             mock.patch.object(server, '_verify_tdesktop_bundle', return_value=(True, '')), \
             mock.patch.object(server, 'get_shared_app', return_value='/current/Telegram.app'), \
             mock.patch.object(server, '_bundle_version', side_effect=version):
            result = server.check_telegram_update('/candidate/Telegram.app')
        self.assertTrue(result['success'])
        self.assertIn('7.3.1', result['warning'])
        self.assertIn('7.2.9', result['warning'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()})

    def test_telegram_upgrade_check_rejects_downgrade_and_warns_when_ayu_unknown(self):
        with mock.patch.object(server, 'is_allowed_app_source', return_value=True), \
             mock.patch.object(server, '_verify_tdesktop_bundle', return_value=(True, '')), \
             mock.patch.object(server, 'get_shared_app', return_value='/current/Telegram.app'), \
             mock.patch.object(server, '_bundle_version', side_effect=lambda p: '7.2.8' if 'candidate' in p else '7.3.1'):
            self.assertFalse(server.check_telegram_update('/candidate/Telegram.app')['success'])
        with mock.patch.object(server, 'is_allowed_app_source', return_value=True), \
             mock.patch.object(server, '_verify_tdesktop_bundle', return_value=(True, '')), \
             mock.patch.object(server, 'get_shared_app', return_value=None), \
             mock.patch.object(server, '_bundle_version', return_value='7.3.1'), \
             mock.patch.object(server, '_bundle_identifier', return_value=None):
            self.assertTrue(server.check_telegram_update('/candidate/Telegram.app')['warning'])

    def test_update_endpoint_requires_acknowledgement_of_current_versions(self):
        responses = []
        class Handler:
            def send_json(self, body): responses.append(body)
        check = {'success': True, 'warning': 'Import incompatible',
                 'acknowledgement': {'telegram_version': '7.3.1', 'ayugram_version': '7.2.9'}}
        with mock.patch.object(server, 'check_telegram_update', return_value=check), \
             mock.patch.object(server, 'update_all_apps', return_value=(True, 'done')) as update:
            for ack in (None, {'telegram_version': '7.3.0', 'ayugram_version': '7.2.9'}):
                server.RequestHandler._post_api_update_all(Handler(), {'source': '/candidate', 'acknowledgement': ack})
                self.assertTrue(responses[-1]['confirmation_required'])
                update.assert_not_called()
            server.RequestHandler._post_api_update_all(Handler(), {'source': '/candidate', 'acknowledgement': check['acknowledgement']})
            update.assert_called_once_with('/candidate')

    def test_frontend_upgrade_warning_does_not_install_until_user_continues(self):
        import subprocess
        source = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources/index.html'
        script = r'''
const text = require('fs').readFileSync(process.argv[1], 'utf8');
const calls = [];
const nodes = {};
const document = {getElementById: id => nodes[id] ||= {classList:{add(){},remove(){}},textContent:''}};
const toast = () => {};
const toastResult = () => {};
const loadSharedAppStatus = () => {};
const refresh = async () => {};
const ack = {telegram_version:'7.3.1',ayugram_version:'7.2.9'};
const api = async (route, method, body) => {
 calls.push([route, body]);
 if (route === '/api/pick-app') return {success:true,path:'/candidate'};
 if (route === '/api/check-telegram-update') return {success:true,warning:'Newer Telegram',acknowledgement:ack};
 return {success:true};
};
eval(text.slice(text.indexOf('let pendingTelegramUpdate ='), text.indexOf('\nasync function backupAccount(')));
(async () => {
 await updateAll();
 if (calls.some(x=>x[0]==='/api/update-all')) throw Error('Installed before acknowledgement');
 cancelTelegramUpdate();
 await confirmTelegramUpdate();
 if (calls.some(x=>x[0]==='/api/update-all')) throw Error('Installed after cancellation');
 await updateAll();
 await confirmTelegramUpdate();
 const last=calls.at(-1);
 if(last[0]!=='/api/update-all'||JSON.stringify(last[1].acknowledgement)!==JSON.stringify(ack)) throw Error('Missing acknowledgement');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_force_is_used_only_after_quit_timeout_for_same_pid(self):
        import itertools
        commands = []
        alive = True
        class InlineThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        def run(command, **kwargs):
            nonlocal alive
            commands.append(command)
            if command == ['kill', '-9', '321']:
                alive = False
            return mock.Mock(returncode=0)
        with mock.patch.object(server, 'find_client_pid', return_value=321), \
             mock.patch.object(server, '_pid_matches_account', side_effect=lambda *args: alive), \
             mock.patch.object(server, '_request_client_quit') as quit_request, \
             mock.patch.object(server, 'find_telegram_pid', return_value=None), \
             mock.patch.object(server.time, 'time', side_effect=itertools.count(0, 10)), \
             mock.patch.object(server.time, 'sleep'), \
             mock.patch.object(server.subprocess, 'run', side_effect=run), \
             mock.patch.object(server.threading, 'Thread', InlineThread):
            self.assertTrue(server.kill_account(str(self.account), client='AyuGram'))
        quit_request.assert_called_once_with(321, str(self.account), 'AyuGram')
        self.assertEqual(commands, [['kill', '321'], ['kill', '-9', '321']])

    def test_new_login_does_not_bypass_newer_or_invalid_copy_headers(self):
        self.assertTrue(self.open(new_login=True)[0])
        for header in (b'TDF$' + struct.pack('<i', 7003001), b'corrupt'):
            (self.copy / 'key_datas').write_bytes(header)
            self.assertFalse(self.open(new_login=True)[0])
            self.assertEqual((self.copy / 'key_datas').read_bytes(), header)

    def test_first_open_copies_session_without_changing_original(self):
        before = hashlib.sha256((self.original / 'key_datas').read_bytes()).digest()
        ok, _ = self.open()
        self.assertTrue(ok)
        self.assertEqual((self.copy / 'key_datas').read_bytes(), b'TDF$' + struct.pack('<i', 7002009) + b'original encrypted session')
        self.assertEqual(hashlib.sha256((self.original / 'key_datas').read_bytes()).digest(), before)
        self.assertNotEqual(os.stat(self.original / 'key_datas').st_ino, os.stat(self.copy / 'key_datas').st_ino)

    def test_custom_name_changes_bundle_and_plist_and_survives_reopen(self):
        master_before = ayugram.bundle_fingerprint(str(self.master))
        original_before = (self.original / 'key_datas').read_bytes()
        with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): 'Norther - Stuart'}}):
            self.assertTrue(self.open()[0])
            app = Path(ayugram.find_app(str(self.account)))
            self.assertEqual(app.name, 'Norther - Stuart.app')
            with (app / 'Contents' / 'Info.plist').open('rb') as f:
                info = plistlib.load(f)
            self.assertEqual(info['CFBundleDisplayName'], 'Norther - Stuart')
            self.assertEqual(info['CFBundleName'], 'Norther - Stuart')
            self.assertEqual((app / 'Contents/MacOS/AyuGram').read_bytes(), b'test executable')
            with mock.patch.object(server, '_copy_app_bundle') as copy:
                self.assertTrue(self.open()[0])
                copy.assert_not_called()
        self.assertEqual(ayugram.bundle_fingerprint(str(self.master)), master_before)
        self.assertEqual((self.original / 'key_datas').read_bytes(), original_before)

    def test_changed_name_renames_existing_clone_without_copying_session_or_app(self):
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        app.rename(self.runtime / 'AyuGram.app')
        self.assertEqual(Path(ayugram.find_app(str(self.account))).name, 'AyuGram.app')
        (self.copy / 'key_datas').write_bytes(b'TDF$' + struct.pack('<i', 7002009) + b'AyuGram changes')
        session = {p.name: p.read_bytes() for p in self.copy.iterdir()}
        original = {p.name: p.read_bytes() for p in self.original.iterdir()}
        stamp = server._read_clone_stamp(str(self.runtime))
        executable_hash = hashlib.sha256(Path(self.exe).read_bytes()).digest()
        with mock.patch('backups._copy_tdata_excluding_cache') as copy_session, \
             mock.patch.object(server, '_copy_app_bundle') as copy_app:
            for name in ('First / Name', 'Second Name'):
                with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): name}}):
                    self.assertTrue(self.open()[0])
                    app = Path(ayugram.find_app(str(self.account)))
                    self.assertEqual(app.name, name.replace('/', '-') + '.app')
                    self.assertEqual(hashlib.sha256(Path(self.exe).read_bytes()).digest(), executable_hash)
                    self.assertEqual(server._read_clone_stamp(str(self.runtime)), stamp)
                    marker = self.runtime / 'externalupdater.d' / 'telegram-manager'
                    self.assertIn(str(app), marker.read_text().splitlines())
            copy_session.assert_not_called()
            copy_app.assert_not_called()
        self.assertEqual(session, {p.name: p.read_bytes() for p in self.copy.iterdir()})
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.original.iterdir()})
        self.assertFalse((self.runtime / 'AyuGram.app').exists())
        self.assertFalse((self.runtime / 'First - Name.app').exists())

    def test_custom_clone_exact_pid_matching_refuses_reopen_while_running(self):
        with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): 'Custom Name'}}):
            self.assertTrue(self.open()[0])
        output = f'321 {self.exe} -ghost\n322 /bin/zsh -c echo {self.exe}\n'
        with mock.patch.object(server, '_get_ps_output', return_value=output), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertEqual(server.find_client_pid(str(self.account), 'AyuGram', fresh=True), 321)
            self.assertTrue(server._pid_matches_account(321, str(self.account), 'AyuGram'))
            self.assertFalse(server._pid_matches_account(322, str(self.account), 'AyuGram'))
            self.assertFalse(ayugram.open_account(str(self.account))[0])
            run.assert_not_called()

    def test_existing_name_destination_is_never_overwritten(self):
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        destination = self.runtime / 'Taken.app'
        destination.write_bytes(b'existing file')
        before = ayugram.bundle_fingerprint(str(app))
        with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): 'Taken'}}), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertFalse(self.open()[0])
            run.assert_not_called()
        self.assertEqual(destination.read_bytes(), b'existing file')
        self.assertEqual(ayugram.bundle_fingerprint(str(app)), before)

    def test_ambiguous_clones_refuse_open_and_do_not_match_a_pid(self):
        import shutil
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        exe = self.exe
        shutil.copytree(app, self.runtime / 'Another.app')
        with self.assertRaisesRegex(ValueError, 'Multiple'):
            ayugram.find_app(str(self.account))
        self.assertEqual(ayugram.executable(str(self.account)), '')
        with mock.patch.object(server, '_get_ps_output', return_value=f'321 {exe}'), \
             mock.patch.object(server.subprocess, 'run') as run:
            self.assertEqual(server.find_client_pid(str(self.account), 'AyuGram', fresh=True), 321)
            self.assertFalse(ayugram.open_account(str(self.account))[0])
            run.assert_not_called()

    def test_symlinked_bundle_or_executable_is_rejected(self):
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        exe = Path(self.exe)
        exe.unlink()
        exe.symlink_to(self.master / 'Contents/MacOS/AyuGram')
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            ayugram.find_app(str(self.account))
        self.assertFalse(self.open()[0])
        exe.unlink()
        exe.write_bytes(b'test executable')
        app.rename(self.runtime / 'saved-bundle')
        app.symlink_to(self.runtime / 'saved-bundle', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            ayugram.find_app(str(self.account))
        self.assertFalse(self.open()[0])

    def test_name_exception_does_not_allow_other_clone_plist_changes(self):
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        path = app / 'Contents/Info.plist'
        with path.open('rb') as f:
            info = plistlib.load(f)
        info['CFBundleExecutable'] = 'malicious'
        with path.open('wb') as f:
            plistlib.dump(info, f)
        with mock.patch.object(server, '_copy_app_bundle', return_value=(False, 'blocked')) as copy:
            self.assertFalse(self.open()[0])
            copy.assert_not_called()

    def test_second_open_keeps_ayugram_changes_instead_of_recopied_original(self):
        self.assertTrue(self.open()[0])
        (self.copy / 'key_datas').write_bytes(b'TDF$' + struct.pack('<i', 7002009) + b'ayugram session changes')
        self.assertTrue(self.open()[0])
        self.assertEqual((self.copy / 'key_datas').read_bytes(), b'TDF$' + struct.pack('<i', 7002009) + b'ayugram session changes')
        self.assertEqual((self.original / 'key_datas').read_bytes(), b'TDF$' + struct.pack('<i', 7002009) + b'original encrypted session')

    def test_common_profile_is_applied_before_launch_and_preserves_original_preferences(self):
        original_settings = {'showPeerId': 0, 'streamerMode': True,
                             'ghostModeSettings': {'0': {'useScheduledMessages': False},
                                                   '123': {'sendReadMessages': True}}}
        path = self.original / 'ayu_settings.json'
        path.write_text(json.dumps(original_settings))
        before = {p.name: p.read_bytes() for p in self.original.iterdir()}
        def launched(command, **kwargs):
            self.assertEqual(command[3:], ['--args', '-noupdate', '-ghost'])
            settings = json.loads((self.copy / 'ayu_settings.json').read_text())
            self.assertTrue(settings['saveDeletedMessages'])
            self.assertTrue(settings['saveMessagesHistory'])
            self.assertFalse(settings['ghostModeSettings']['0']['sendReadMessages'])
            self.assertFalse(settings['ghostModeSettings']['0']['markReadAfterAction'])
            self.assertTrue(settings['useGlobalGhostMode'])
            self.assertEqual(settings['showPeerId'], 2)
            self.assertEqual(settings['translationProvider'], 'telegram')
            self.assertTrue(settings['streamerMode'])
            self.assertEqual(settings['ghostModeSettings']['123'], {'sendReadMessages': True})
            return mock.Mock(returncode=0, stderr=b'')
        self.assertTrue(self.open(on_launch=launched)[0])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.original.iterdir()})

    def test_profile_is_not_reapplied_over_user_changes_on_reopen(self):
        self.assertTrue(self.open()[0])
        path = self.copy / 'ayu_settings.json'
        settings = json.loads(path.read_text())
        settings['showPeerId'] = 1
        settings['ghostModeSettings']['0']['sendReadMessages'] = True
        path.write_text(json.dumps(settings))
        before = path.read_bytes()
        self.assertTrue(self.open()[0])
        self.assertEqual(path.read_bytes(), before)

    def test_erased_global_ghost_entry_is_repaired_on_reopen_without_resetting_other_preferences(self):
        self.assertTrue(self.open(new_login=True)[0])
        path = self.copy / 'ayu_settings.json'
        settings = json.loads(path.read_text())
        settings.update(ghostModeSettings={}, showPeerId=1, saveDeletedMessages=False)
        path.write_text(json.dumps(settings))
        def launched(command):
            self.assertIn('-ghost', command)
            actual = json.loads(path.read_text())
            self.assertEqual(actual['ghostModeSettings']['0'], ayugram.COMMON_PROFILE['ghostModeSettings']['0'])
            self.assertEqual(actual['showPeerId'], 1)
            self.assertFalse(actual['saveDeletedMessages'])
        self.assertTrue(self.open(on_launch=launched)[0])
        self.assertTrue(self.open(on_launch=launched)[0])

    def test_erased_ghost_entry_is_not_repaired_when_user_disabled_global_mode(self):
        self.assertTrue(self.open(new_login=True)[0])
        path = self.copy / 'ayu_settings.json'
        settings = json.loads(path.read_text())
        settings.update(ghostModeSettings={}, useGlobalGhostMode=False)
        path.write_text(json.dumps(settings))
        before = path.read_bytes()
        self.assertTrue(self.open(on_launch=lambda command: self.assertNotIn('-ghost', command))[0])
        self.assertEqual(path.read_bytes(), before)

    def test_updated_profile_enables_message_storage_on_an_existing_copy(self):
        previous = {key: value for key, value in ayugram.COMMON_PROFILE.items()
                    if key not in ('saveDeletedMessages', 'saveMessagesHistory')}
        with mock.patch.object(ayugram, 'COMMON_PROFILE', previous):
            self.assertTrue(self.open()[0])
        path = self.copy / 'ayu_settings.json'
        settings = json.loads(path.read_text())
        settings.update(saveDeletedMessages=False, saveMessagesHistory=False, streamerMode=True)
        path.write_text(json.dumps(settings))
        original = (self.original / 'key_datas').read_bytes()
        copied = (self.copy / 'key_datas').read_bytes()
        self.assertTrue(self.open()[0])
        updated = json.loads(path.read_text())
        self.assertTrue(updated['saveDeletedMessages'])
        self.assertTrue(updated['saveMessagesHistory'])
        self.assertTrue(updated['streamerMode'])
        self.assertEqual((self.original / 'key_datas').read_bytes(), original)
        self.assertEqual((self.copy / 'key_datas').read_bytes(), copied)

    def test_invalid_existing_settings_are_preserved_and_launch_is_refused(self):
        self.copy.mkdir(parents=True)
        (self.copy / 'key_datas').write_bytes((self.original / 'key_datas').read_bytes())
        path = self.copy / 'ayu_settings.json'
        for raw in ('not json', '[]', '{"ghostModeSettings": []}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertFalse(self.open()[0])
                self.assertEqual(path.read_text(), raw)
                self.assertFalse((self.runtime / '.common-profile.json').exists())

    def test_profile_cannot_write_through_symlink_or_hardlink_to_original(self):
        original = self.original / 'ayu_settings.json'
        original.write_text('{"showPeerId": 0}')
        self.copy.mkdir(parents=True)
        (self.copy / 'key_datas').write_bytes((self.original / 'key_datas').read_bytes())
        for name in ('ayu_settings.json', 'ayu_settings.json.tmp'):
            for link in ('symlink', 'hardlink'):
                with self.subTest(name=name, link=link):
                    target = self.copy / name
                    if link == 'symlink':
                        target.symlink_to(original)
                    else:
                        os.link(original, target)
                    self.assertFalse(self.open()[0])
                    self.assertEqual(original.read_text(), '{"showPeerId": 0}')
                    target.unlink()

    def test_open_refuses_when_ayugram_is_running(self):
        with mock.patch.object(server, 'find_client_pid', return_value=123), \
             mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertFalse(self.runtime.exists())

    def test_unknown_process_state_never_starts_a_second_client(self):
        with mock.patch.object(server, '_get_ps_output', return_value=None):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertFalse(self.runtime.exists())

    def test_symlinked_copy_destination_is_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.runtime.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_session_copy_is_not_treated_as_a_valid_session(self):
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'), \
             mock.patch('backups._copy_tdata_excluding_cache', return_value=(False, 'disk full')), \
             mock.patch.object(server.subprocess, 'run', return_value=mock.Mock(returncode=0, stderr=b'')):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertFalse(self.copy.exists())
        self.assertEqual((self.original / 'key_datas').read_bytes(), b'TDF$' + struct.pack('<i', 7002009) + b'original encrypted session')

    def test_process_lookup_and_close_identity_include_the_ayugram_copy(self):
        output = f'321 {self.exe}\n322 /bin/zsh -c echo {self.exe}\n'
        with mock.patch.object(server, '_get_ps_output', return_value=output):
            self.assertEqual(server.find_telegram_pid(str(self.account)), 321)
            self.assertTrue(server._pid_matches_account(321, str(self.account)))
        with mock.patch.object(server, '_get_ps_output', return_value=f'321 /Applications/AyuGram.app/Contents/MacOS/AyuGram'):
            self.assertIsNone(server.find_telegram_pid(str(self.account)))

    def test_endpoint_rejects_paths_outside_accounts(self):
        responses = []
        class Handler:
            def send_json(self, body, *args):
                responses.append(body)
        server.RequestHandler._post_api_open_ayugram(Handler(), {'path': '/tmp/not-an-account'})
        self.assertFalse(responses[0]['success'])

    def test_missing_master_cannot_start_an_empty_login(self):
        import shutil
        shutil.rmtree(self.master)
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertFalse(self.copy.exists())

    def test_changed_library_in_unsigned_master_is_rejected(self):
        library = self.master / 'Contents' / 'Frameworks' / 'changed.dylib'
        library.parent.mkdir()
        library.write_bytes(b'not in the official release')
        with mock.patch.object(server, '_get_ps_output', return_value='1 /bin/sleep'):
            ok, _ = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertFalse(self.copy.exists())

    def test_ayugram_import_does_not_depend_on_server_being_imported_first(self):
        import subprocess
        result = subprocess.run([sys.executable, '-c', 'import ayugram'],
                                cwd=str(Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources'),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frontend_sends_each_client_to_its_correct_endpoint(self):
        import subprocess
        source = Path(__file__).resolve().parents[1] / 'TelegramManager.app/Contents/Resources/index.html'
        script = r'''
const fs = require('fs');
const text = fs.readFileSync(process.argv[1], 'utf8');
const calls = [];
const api = async (endpoint, method, data) => { calls.push([endpoint, method, data.path]); return {success:true}; };
const toast = () => {};
const setTimeout = () => {};
eval(text.slice(text.indexOf('const _openingPaths ='), text.indexOf('\nasync function openAll()')));
(async () => {
 await openAccount('/account');
 await openAccount('/account', 'AyuGram');
 await openAccount('/account', 'AyuGram', true);
 if (JSON.stringify(calls) !== JSON.stringify([
  ['/api/open','POST','/account'], ['/api/open-ayugram','POST','/account'],
  ['/api/new-ayugram-login','POST','/account']
 ])) process.exit(1);
})();
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_newer_source_is_rejected_before_ayugram_can_reset_the_copy(self):
        original = b'TDF$' + struct.pack('<i', 7003001) + b'newer encrypted session'
        (self.original / 'key_datas').write_bytes(original)
        ok, message = self.open()
        self.assertFalse(ok)
        self.assertIn('7.3.1', message)
        self.assertFalse(self.runtime.exists())
        self.assertEqual((self.original / 'key_datas').read_bytes(), original)

    def test_newer_persistent_copy_is_never_downgraded(self):
        self.copy.mkdir(parents=True)
        session = b'TDF$' + struct.pack('<i', 7003001) + b'newer AyuGram session'
        (self.copy / 'key_datas').write_bytes(session)
        ok, _ = self.open()
        self.assertFalse(ok)
        self.assertEqual((self.copy / 'key_datas').read_bytes(), session)

    def test_invalid_session_header_does_not_launch_an_empty_login(self):
        for raw in (b'not a session', b'TDF$' + struct.pack('<i', -1)):
            with self.subTest(header=raw):
                (self.original / 'key_datas').write_bytes(raw)
                ok, _ = self.open()
                self.assertFalse(ok)
                self.assertFalse(self.runtime.exists())

    def use_inline_master(self, kind='local-inline'):
        executable = self.master / 'Contents/MacOS/AyuGram'
        executable.rename(executable.with_name(ayugram.INLINE_EXECUTABLE))
        path = self.master / 'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info.update(CFBundleIdentifier=ayugram.INLINE_BUNDLE_ID,
                    CFBundleExecutable=ayugram.INLINE_EXECUTABLE,
                    CFBundleName='AyuGram Inline Test', CFBundleDisplayName='AyuGram Inline Test')
        path.write_bytes(plistlib.dumps(info))
        state._save_json_atomic(str(self.master.parent / '.ayugram-official.json'),
                                {'kind': kind, 'fingerprint': ayugram.bundle_fingerprint(str(self.master))})

    def test_inline_custom_name_keeps_signed_plist_and_binary_and_reopen_history(self):
        self.use_inline_master()
        master_before = ayugram.bundle_fingerprint(str(self.master))
        plist_before = (self.master / 'Contents/Info.plist').read_bytes()
        original_before = (self.original / 'key_datas').read_bytes()
        launches = []
        with mock.patch.dict(state.metadata, {'dock_names': {str(self.account): 'Custom / Account'}}):
            self.assertTrue(self.open(on_launch=launches.append)[0])
            app = Path(ayugram.find_app(str(self.account)))
            self.assertEqual(app.name, 'Custom - Account.app')
            self.assertEqual((app / 'Contents/Info.plist').read_bytes(), plist_before)
            self.assertEqual((app / 'Contents/MacOS' / ayugram.INLINE_EXECUTABLE).read_bytes(), b'test executable')
            session = b'TDF$' + struct.pack('<i', 7002009) + b'inline session'
            (self.copy / 'key_datas').write_bytes(session)
            history = self.copy / 'synthetic-history.db'
            history.write_bytes(b'local message history')
            settings_path = self.copy / 'ayu_settings.json'
            settings = json.loads(settings_path.read_text())
            self.assertTrue(settings['semiTransparentDeletedMessages'])
            settings.update(semiTransparentDeletedMessages=False, showPeerId=1)
            settings_path.write_text(json.dumps(settings))
            with mock.patch.object(server, '_copy_app_bundle') as copy_app, \
                 mock.patch('backups._copy_tdata_excluding_cache') as copy_session:
                self.assertTrue(self.open(on_launch=launches.append)[0])
            copy_app.assert_not_called()
            copy_session.assert_not_called()
            self.assertEqual(json.loads(settings_path.read_text()), settings)
            self.assertEqual((self.copy / 'key_datas').read_bytes(), session)
            self.assertEqual(history.read_bytes(), b'local message history')
        for command in launches:
            self.assertEqual(command[command.index('-workdir') + 1], str(self.copy.parent.resolve()))
        self.assertEqual((self.copy.parent / '.ayugram-inline-test').read_bytes(),
                         b'ayugram-inline-isolated-profile-v1\n')
        self.assertEqual(ayugram.bundle_fingerprint(str(self.master)), master_before)
        self.assertEqual((self.original / 'key_datas').read_bytes(), original_before)

    def test_inline_refresh_keeps_existing_session_history_and_preferences(self):
        self.assertTrue(self.open()[0])
        history = self.copy / 'synthetic-history.db'
        history.write_bytes(b'existing history')
        session = (self.copy / 'key_datas').read_bytes()
        path = self.copy / 'ayu_settings.json'
        settings = json.loads(path.read_text())
        settings.update(showPeerId=1, saveDeletedMessages=False)
        path.write_text(json.dumps(settings))
        self.use_inline_master()
        with mock.patch('backups._copy_tdata_excluding_cache') as copy:
            self.assertTrue(self.open()[0])
        copy.assert_not_called()
        self.assertEqual((self.copy / 'key_datas').read_bytes(), session)
        self.assertEqual(history.read_bytes(), b'existing history')
        updated = json.loads(path.read_text())
        self.assertEqual(updated['showPeerId'], 1)
        self.assertFalse(updated['saveDeletedMessages'])
        self.assertTrue(updated['semiTransparentDeletedMessages'])
        self.assertTrue(self.exe.endswith('/' + ayugram.INLINE_EXECUTABLE))

    def test_inline_bootstrap_also_uses_marked_workdir(self):
        self.use_inline_master()
        bootstrap = []
        def launched(command):
            bootstrap.append(command)
            self.assertEqual(command[command.index('-workdir') + 1], str(self.copy.parent.resolve()))
            self.assertEqual((self.copy.parent / '.ayugram-inline-test').read_bytes(),
                             b'ayugram-inline-isolated-profile-v1\n')
        self.assertTrue(self.open(new_login=True, on_bootstrap=launched)[0])
        self.assertEqual(len(bootstrap), 1)

    def test_inline_refresh_repairs_changed_signed_display_name(self):
        self.use_inline_master()
        self.assertTrue(self.open()[0])
        app = Path(ayugram.find_app(str(self.account)))
        path = app / 'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info['CFBundleDisplayName'] = 'Tampered sealed name'
        path.write_bytes(plistlib.dumps(info))
        history = self.copy / 'synthetic-history.db'
        history.write_bytes(b'preserve history during repair')
        self.assertTrue(self.open()[0])
        self.assertEqual(path.read_bytes(), (self.master / 'Contents/Info.plist').read_bytes())
        self.assertEqual(history.read_bytes(), b'preserve history during repair')

    def test_inline_requires_explicit_local_manifest(self):
        self.use_inline_master(kind='official')
        self.assertFalse(self.open()[0])
        self.assertFalse(self.copy.exists())
        (self.master.parent / '.ayugram-official.json').unlink()
        self.assertFalse(self.open()[0])
        self.assertFalse(self.copy.exists())

    def test_inline_refuses_newer_persistent_session_without_changing_history(self):
        self.use_inline_master()
        self.copy.mkdir(parents=True)
        session = b'TDF$' + struct.pack('<i', 7003001) + b'newer inline session'
        (self.copy / 'key_datas').write_bytes(session)
        history = self.copy / 'synthetic-history.db'
        history.write_bytes(b'preserved history')
        self.assertFalse(self.open()[0])
        self.assertEqual((self.copy / 'key_datas').read_bytes(), session)
        self.assertEqual(history.read_bytes(), b'preserved history')

    def test_inline_rejects_wrong_marker_and_linked_profile_files(self):
        self.use_inline_master()
        self.copy.mkdir(parents=True)
        (self.copy / 'key_datas').write_bytes((self.original / 'key_datas').read_bytes())
        marker = self.copy.parent / '.ayugram-inline-test'
        marker.write_text('wrong marker\n')
        self.assertFalse(self.open()[0])
        self.assertEqual(marker.read_text(), 'wrong marker\n')
        marker.unlink()
        outside = Path(self.temp.name) / 'outside-history'
        outside.write_bytes(b'must not change')
        linked = self.copy / 'linked-history'
        for link in (lambda: linked.symlink_to(outside), lambda: os.link(outside, linked),
                     lambda: os.mkfifo(linked)):
            with self.subTest(link=link):
                link()
                self.assertFalse(self.open()[0])
                self.assertEqual(outside.read_bytes(), b'must not change')
                linked.unlink()

    def test_inline_bundle_executable_cannot_be_selected_by_untrusted_plist_value(self):
        self.use_inline_master()
        path = self.master / 'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        for changes in ({'CFBundleExecutable': '../../outside'},
                        {'CFBundleIdentifier': 'attacker.client'}):
            with self.subTest(changes=changes):
                path.write_bytes(plistlib.dumps(dict(info, **changes)))
                with self.assertRaises(ValueError):
                    ayugram._bundle_executable(str(self.master))
        path.write_bytes(plistlib.dumps(['not a bundle dictionary']))
        with self.assertRaises(ValueError):
            ayugram._bundle_executable(str(self.master))

    def test_inline_pid_matches_exact_account_even_after_clone_is_removed(self):
        self.use_inline_master()
        self.assertTrue(self.open()[0])
        executable = self.exe
        output = (f'321 {executable} -workdir {self.copy.parent}\n'
                  f'322 /bin/zsh -c echo {executable}\n'
                  '323 /Applications/AyuGram Inline Test.app/Contents/MacOS/AyuGram Inline Test\n')
        self.assertEqual(server._client_pid_from_ps_output(str(self.account), output, 'AyuGram'), 321)
        import shutil
        shutil.rmtree(ayugram.find_app(str(self.account)))
        self.assertEqual(server._client_pid_from_ps_output(str(self.account), output, 'AyuGram'), 321)
        self.assertIsNone(server._client_pid_from_ps_output(str(self.account), output.split('\n', 1)[1], 'AyuGram'))

    def test_inline_close_keeps_telegram_separate_and_detects_old_removed_clone(self):
        self.assertTrue(self.open()[0])
        old_executable = self.exe
        self.use_inline_master()
        self.assertTrue(self.open()[0])
        telegram = self.account / 'Telegram.app' / 'Contents/MacOS/Telegram'
        telegram.parent.mkdir(parents=True)
        telegram.write_bytes(b'telegram executable')
        output = f'456 {telegram}\n321 {self.exe} -workdir {self.copy.parent}'
        with mock.patch.object(server, '_get_ps_output', return_value=output), \
             mock.patch.object(server, '_request_client_quit') as quit_request, \
             mock.patch.object(server.subprocess, 'run') as run, \
             mock.patch.object(server.threading, 'Thread'):
            self.assertTrue(server.kill_account(str(self.account), client='AyuGram'))
            quit_request.assert_called_with(321, str(self.account), 'AyuGram')
            self.assertTrue(server.kill_account(str(self.account), client='Telegram'))
            quit_request.assert_called_with(456, str(self.account), 'Telegram')
            self.assertFalse(server._pid_matches_account(456, str(self.account), 'AyuGram'))
            run.assert_not_called()
        self.assertEqual(server._client_pid_from_ps_output(
            str(self.account), f'789 {old_executable}', 'AyuGram'), 789)
