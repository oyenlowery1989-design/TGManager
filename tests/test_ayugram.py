"""Exercise verified-release setup and the separate optional client path."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "TelegramManager.app/Contents/Resources"))
os.environ.setdefault("TG_SESSION_TOKEN", "unit-test-token")
server = importlib.import_module("server")
state = importlib.import_module("state")
ayugram = importlib.import_module("ayugram")


class AyuGramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.account = self.root / "accounts/account"
        (self.account / "TelegramForcePortable/tdata").mkdir(parents=True)
        (self.account / "TelegramForcePortable/tdata/session").write_text("original session")
        self.source = self.root / "official/AyuGram.app"
        (self.source / "Contents/MacOS").mkdir(parents=True)
        (self.source / "Contents/Frameworks").mkdir()
        (self.source / "Contents/MacOS/AyuGram").write_bytes(b"test executable")
        (self.source / "Contents/Frameworks/library.dylib").write_bytes(b"test library")
        (self.source / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "one.ayugram.AyuGramDesktop", "CFBundleExecutable": "AyuGram"}))
        manifest = {"version": "7.2.9", "source": "https://github.com/AyuGram/AyuGramDesktop/releases/download/v7.2.9/AyuGram.dmg",
                    "files": {p.relative_to(self.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in self.source.rglob("*") if p.is_file()}}
        self.patchers = [mock.patch.object(state, "DATA_DIR", str(self.root / "data")),
                         mock.patch.object(state, "ROOT_DIR", str(self.root / "accounts")),
                         mock.patch("ayugram.release_manifest", return_value=manifest)]
        for patcher in self.patchers:
            patcher.start()
        self.original_metadata = copy.deepcopy(state.metadata)
        self.last_opened = state.metadata.get("last_opened", {}).copy()
        self.binary = str(Path(ayugram.clone_path(str(self.account))) / "Contents/MacOS/AyuGram")

    def tearDown(self):
        server._clear_watcher_exempt(str(self.account))
        state.metadata.clear()
        state.metadata.update(self.original_metadata)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def copy_bundle(self, source, destination):
        target = Path(destination)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return True, ""

    def install_fixture(self):
        master = Path(ayugram.master_path())
        master.parent.mkdir(parents=True)
        shutil.copytree(self.source, master)
        return master

    def test_manifest_rejects_modified_binary_library_and_added_files(self):
        self.assertEqual(ayugram.verify_bundle(self.source), (True, ""))
        binary = self.source / "Contents/MacOS/AyuGram"
        binary.write_bytes(b"modified binary")
        self.assertFalse(ayugram.verify_bundle(self.source)[0])
        binary.write_bytes(b"test executable")
        library = self.source / "Contents/Frameworks/library.dylib"
        library.write_bytes(b"modified library")
        self.assertFalse(ayugram.verify_bundle(self.source)[0])
        library.write_bytes(b"test library")
        (self.source / "Contents/Frameworks/extra.dylib").write_bytes(b"extra code")
        self.assertFalse(ayugram.verify_bundle(self.source)[0])

    def test_manifest_rejects_missing_files_and_symlink_files_or_directories(self):
        binary = self.source / "Contents/MacOS/AyuGram"
        binary.unlink()
        self.assertFalse(ayugram.verify_bundle(self.source)[0])
        binary.symlink_to(self.source / "Contents/Frameworks/library.dylib")
        self.assertFalse(ayugram.verify_bundle(self.source)[0])
        binary.unlink()
        binary.write_bytes(b"test executable")
        (self.source / "Contents/outside").symlink_to(self.root, target_is_directory=True)
        self.assertFalse(ayugram.verify_bundle(self.source)[0])

    def test_setup_preserves_the_telegram_master(self):
        telegram = Path(ayugram.master_path()).with_name("Telegram.app")
        telegram.mkdir(parents=True)
        (telegram / "preserved").write_text("Telegram master")
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[]), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle):
            ok, message = ayugram.install(str(self.source))
        self.assertTrue(ok, message)
        self.assertEqual((telegram / "preserved").read_text(), "Telegram master")
        self.assertTrue(ayugram.verify_bundle(ayugram.master_path())[0])

    def test_setup_failure_keeps_previous_master(self):
        master = self.install_fixture()
        (self.source / "Contents/MacOS/AyuGram").write_bytes(b"modified source")
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[]), \
             mock.patch("server._copy_app_bundle") as copy:
            self.assertFalse(ayugram.install(str(self.source))[0])
        copy.assert_not_called()
        self.assertTrue(ayugram.verify_bundle(master)[0])

    def test_setup_swap_failure_rolls_back_verified_master(self):
        master = self.install_fixture()
        original_rename = os.rename

        def fail_final(source, destination):
            if str(source).endswith("/AyuGram.app") and str(destination) == str(master):
                raise OSError("disk failure")
            return original_rename(source, destination)

        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[]), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.os.rename", side_effect=fail_final):
            self.assertFalse(ayugram.install(str(self.source))[0])
        self.assertTrue(ayugram.verify_bundle(master)[0])

    def test_setup_rejects_symlinked_shared_directory_and_preserves_user_files(self):
        data = Path(state.DATA_DIR)
        data.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (data / "_apps").symlink_to(outside, target_is_directory=True)
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[]):
            self.assertFalse(ayugram.install(str(self.source))[0])
        self.assertEqual(list(outside.iterdir()), [])
        (data / "_apps").unlink()
        target = Path(ayugram.master_path())
        target.parent.mkdir(parents=True)
        target.write_text("existing user file")
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[]):
            self.assertFalse(ayugram.install(str(self.source))[0])
        self.assertEqual(target.read_text(), "existing user file")

    def test_existing_backup_restore_preserves_ayugram_settings(self):
        backups = importlib.import_module("backups")
        tdata = self.account / "TelegramForcePortable/tdata"
        settings = tdata / "ayu_settings.json"
        settings.write_text('{"ghostMode": true}')
        with mock.patch.object(state, "METADATA_FILE", str(self.root / "private/metadata.json")), \
             mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.scan_accounts", return_value=[{"path": str(self.account)}]), \
             mock.patch.dict(state.config, {"backup_keep_per_account": 0}):
            ok, message, location = backups.backup_account(str(self.account), "account")
            self.assertTrue(ok, message)
            settings.write_text('{"ghostMode": false}')
            ok, message = backups.restore_backup(location, str(self.account))
            self.assertTrue(ok, message)
        self.assertEqual(json.loads(settings.read_text()), {"ghostMode": True})
        self.assertEqual((tdata / "session").read_text(), "original session")

    def test_setup_rejects_master_replacement_while_ayugram_runs(self):
        with mock.patch("server._get_ps_output", return_value=f"321 {self.binary}"), \
             mock.patch("server.scan_accounts", return_value=[{"path": str(self.account)}]), \
             mock.patch("ayugram.verify_bundle") as verify:
            self.assertFalse(ayugram.install(str(self.source))[0])
        verify.assert_not_called()

    def test_default_telegram_lookup_still_ignores_ayugram_bundle(self):
        self.copy_bundle(self.source, ayugram.clone_path(str(self.account)))
        self.assertIsNone(server.find_account_app(str(self.account)))
        (self.account / "Telegram.app").mkdir()
        self.assertEqual(server.find_account_app(str(self.account)), str(self.account / "Telegram.app"))

    def test_running_ayugram_is_visible_even_when_a_telegram_bundle_exists(self):
        telegram = self.account / "Telegram.app"
        telegram.mkdir()
        output = f"321 {self.binary} -autostart\n322 {self.binary}-unrelated"
        self.assertEqual(server._telegram_pid_from_ps_output(str(self.account), output, str(telegram)), 321)
        other = self.root / "accounts/another"
        self.assertIsNone(ayugram.pid_from_ps(str(other), output))
        self.assertIsNone(ayugram.pid_from_ps(str(self.account), f"1 /bin/sh -c {self.binary}"))
        with mock.patch("server._get_ps_output", return_value=output):
            self.assertTrue(server._pid_matches_account(321, str(self.account)))
            self.assertFalse(server._pid_matches_account(322, str(self.account)))

    def test_telegram_open_refuses_running_ayugram(self):
        self.copy_bundle(self.source, ayugram.clone_path(str(self.account)))
        with mock.patch("server._get_ps_output", return_value=f"321 {self.binary}"), \
             mock.patch("server.subprocess.run") as run:
            ok, message = server.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertIn("Close AyuGram", message)
        run.assert_not_called()

    def test_ayugram_refuses_running_telegram_and_unknown_process_state(self):
        telegram = self.account / "Telegram.app"
        telegram.mkdir()
        output = f"11 {telegram}/Contents/MacOS/Telegram"
        for processes in (output, None):
            with mock.patch("server._get_ps_output", return_value=processes), \
                 mock.patch("server.backup_account") as backup, \
                 mock.patch("ayugram.subprocess.run") as run:
                self.assertFalse(ayugram.open_account(str(self.account))[0])
            backup.assert_not_called()
            run.assert_not_called()

    def test_backup_failure_prevents_launch(self):
        self.install_fixture()
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.backup_account", return_value=(False, "disk full", "")), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.subprocess.run") as run:
            self.assertFalse(ayugram.open_account(str(self.account))[0])
        run.assert_not_called()

    def test_tampered_master_prevents_launch(self):
        master = self.install_fixture()
        (master / "Contents/MacOS/AyuGram").write_bytes(b"tampered")
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.backup_account", return_value=(True, "saved", "/backup")), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.subprocess.run") as run:
            self.assertFalse(ayugram.open_account(str(self.account))[0])
        run.assert_not_called()

    def test_successful_launch_preserves_telegram_and_session_and_waits_for_pid(self):
        self.install_fixture()
        telegram = self.account / "Telegram.app"
        telegram.mkdir()
        (telegram / "keep").write_text("Telegram clone")
        stages = []
        def backup(*args):
            stages.append("backup")
            return True, "saved", "/backup"
        def launch(args, **kwargs):
            self.assertIn("backup", stages)
            self.assertEqual(args, ["open", "-n", ayugram.clone_path(str(self.account))])
            self.assertTrue(ayugram.verify_bundle(args[-1])[0])
            stages.append("launch")
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("server._get_ps_output", side_effect=["100 /bin/sleep 1", "100 /bin/sleep 1", f"321 {self.binary}"]), \
             mock.patch("server.backup_account", side_effect=backup), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.subprocess.run", side_effect=launch), \
             mock.patch("state.save_metadata"):
            ok, message = ayugram.open_account(str(self.account))
        self.assertTrue(ok, message)
        self.assertEqual(stages, ["backup", "launch"])
        self.assertEqual((telegram / "keep").read_text(), "Telegram clone")
        self.assertEqual((self.account / "TelegramForcePortable/tdata/session").read_text(), "original session")

    def test_external_start_during_preparation_prevents_second_launch(self):
        self.install_fixture()
        telegram = self.account / "Telegram.app"
        telegram.mkdir()
        with mock.patch("server._get_ps_output", side_effect=["100 /bin/sleep 1", f"11 {telegram}/Contents/MacOS/Telegram"]), \
             mock.patch("server.backup_account", return_value=(True, "saved", "/backup")), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.subprocess.run") as run:
            self.assertFalse(ayugram.open_account(str(self.account))[0])
        run.assert_not_called()

    def test_same_account_operation_lock_blocks_parallel_launch(self):
        result = []
        with state._account_path_lock(str(self.account)):
            worker = threading.Thread(target=lambda: result.append(ayugram.open_account(str(self.account))))
            worker.start()
            worker.join(2)
        self.assertEqual(result, [(False, state._BUSY_MSG)])

    def test_ayugram_watcher_cleanup_does_not_delete_a_live_client_or_session(self):
        self.install_fixture()
        app = ayugram.clone_path(str(self.account))
        self.copy_bundle(self.source, app)
        with mock.patch("server.get_shared_app", return_value=None), \
             mock.patch("server._get_ps_output", return_value=f"321 {self.binary}"):
            self.assertFalse(server._remove_clone_if_idle(str(self.account)))
        self.assertTrue(Path(app).is_dir())
        with mock.patch("server.get_shared_app", return_value=None), \
             mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"):
            self.assertTrue(server._remove_clone_if_idle(str(self.account)))
        self.assertFalse(Path(app).exists())
        self.assertEqual((self.account / "TelegramForcePortable/tdata/session").read_text(), "original session")

    def test_scan_reports_ayugram_running_without_claiming_telegram_is_installed(self):
        with mock.patch.object(server, "ROOT_DIR", str(self.account.parent)), \
             mock.patch("server.get_shared_app", return_value=None), \
             mock.patch("server._get_ps_output", return_value=f"321 {self.binary}"):
            accounts = server.scan_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["running"])
        self.assertEqual(accounts[0]["running_client"], "AyuGram")
        self.assertFalse(accounts[0]["has_app"])

    def test_timeout_does_not_claim_the_client_started(self):
        self.install_fixture()
        with mock.patch("server._get_ps_output", return_value="100 /bin/sleep 1"), \
             mock.patch("server.backup_account", return_value=(True, "saved", "/backup")), \
             mock.patch("server._copy_app_bundle", side_effect=self.copy_bundle), \
             mock.patch("ayugram.subprocess.run", return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch("ayugram.time.sleep"):
            ok, message = ayugram.open_account(str(self.account))
        self.assertFalse(ok)
        self.assertIn("did not start", message)
        self.assertEqual(state.metadata.get("last_opened", {}), self.last_opened)

    def test_running_ayugram_is_idempotent_and_does_not_start_another_instance(self):
        with mock.patch("server._get_ps_output", return_value=f"321 {self.binary}"), \
             mock.patch("server.backup_account") as backup, \
             mock.patch("server._copy_app_bundle") as copy:
            self.assertEqual(ayugram.open_account(str(self.account)), (True, "already running"))
        backup.assert_not_called()
        copy.assert_not_called()

    def test_dashboard_keeps_telegram_action_and_routes_optional_button_separately(self):
        script = r'''
const fs = require("fs"), assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
const _keeperInfo = { enabled: false };
const _openingPaths = new Set();
const requests = [];
let refreshes = 0;
const toast = () => {};
const refresh = async () => { refreshes++; };
const api = async (route, method, body) => {
  requests.push({route, method, body});
  return {success: true};
};
const code = source.slice(source.indexOf("function esc(s)"), source.indexOf("function getFilteredAccounts()"))
  + source.slice(source.indexOf("const COLOR_PALETTE"), source.indexOf("let _statsData"))
  + source.slice(source.indexOf("async function openWithAyuGram(path)"), source.indexOf("async function openAll()"));
eval(code + `
const acc = {name: "Account", path: "/accounts/one", rel_path: "Account", status: "ready", running: false, can_backup: true};
const html = buildCard(acc);
assert.match(html, /openAccount[(]/);
assert.match(html, /openWithAyuGram[(]/);
assert.match(html, /Open with AyuGram/);
const live = buildCard({...acc, running: true, running_client: "AyuGram"});
assert.match(live, /AyuGram running/);
assert.match(live, /closeAccount[(]/);
assert.doesNotMatch(live, /openWithAyuGram[(]/);
assert.doesNotMatch(buildCard({...acc, status: "no_data", can_backup: false}), /openWithAyuGram[(]/);
(async () => {
  await openWithAyuGram(acc.path);
  assert.deepEqual(requests, [{route: "/api/open-ayugram", method: "POST", body: {path: acc.path}}]);
  assert.equal(refreshes, 1);
  assert.equal(_openingPaths.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
`);
'''
        source = Path(__file__).resolve().parents[1] / "TelegramManager.app/Contents/Resources/index.html"
        result = subprocess.run(["node", "-e", script, str(source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
