"""Regression coverage for the pre-AyuGram audit's reproducible failures."""
import copy
import importlib
import json
import os
from pathlib import Path
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
proxy = importlib.import_module("proxy")
backups = importlib.import_module("backups")


class RelocationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = str(self.root / "old")
        self.new = str(self.root / "new")
        Path(self.old).mkdir()
        self.saved = copy.deepcopy(state.metadata)
        state.metadata.clear()
        state.metadata.update({"notes": {self.old: "original"}})

    def tearDown(self):
        state.metadata.clear()
        state.metadata.update(self.saved)
        self.tmp.cleanup()

    def test_concurrent_edit_waits_for_relocation_and_survives(self):
        started = threading.Event()
        finished = threading.Event()

        def edit():
            started.set()
            with state._meta_lock:
                state.metadata["notes"]["unrelated-account"] = "new edit"
            finished.set()

        worker = threading.Thread(target=edit)

        def persist(meta):
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1), "metadata lock released before commit")

        try:
            with mock.patch("server.load_workspaces", return_value={}), \
                 mock.patch("server.save_metadata", side_effect=persist), \
                 mock.patch("server.save_workspaces"), \
                 mock.patch("server.set_telegram_display_name"):
                ok, result = server._relocate_account(self.old, self.new)
            self.assertTrue(ok)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(state.metadata["notes"]["unrelated-account"], "new edit")
            self.assertEqual(state.metadata["notes"][self.new], "original")
        finally:
            if worker.ident is not None:
                worker.join(2)

    def test_workspace_write_failure_restores_folder_and_metadata(self):
        writes = []
        with mock.patch("server.load_workspaces", return_value={}), \
             mock.patch("server.save_metadata", side_effect=lambda value: writes.append(copy.deepcopy(value))), \
             mock.patch("server.save_workspaces", side_effect=[OSError("disk full"), None]), \
             mock.patch("server.set_telegram_display_name"):
            ok, message = server._relocate_account(self.old, self.new)
        self.assertFalse(ok)
        self.assertTrue(Path(self.old).is_dir())
        self.assertFalse(Path(self.new).exists())
        self.assertEqual(state.metadata["notes"], {self.old: "original"})
        self.assertEqual(writes[-1]["notes"], {self.old: "original"})


class ProxyBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = mock.patch.object(state, "DATA_DIR", self.tmp.name)
        self.data.start()

    def tearDown(self):
        self.data.stop()
        self.tmp.cleanup()

    def _query(self, args, **kwargs):
        host, port, enabled = ("old-http", "8080", "Yes") if args[1] == "-getwebproxy" else ("old-https", "8443", "No")
        return subprocess.CompletedProcess(args, 0, stdout=f"Enabled: {enabled}\nServer: {host}\nPort: {port}\nAuthenticated Proxy Enabled: 0\n", stderr="")

    def test_failed_apply_keeps_distinct_protocol_baselines(self):
        with mock.patch("proxy.get_active_network_service", return_value="Wi-Fi"), \
             mock.patch("proxy.subprocess.run", side_effect=self._query), \
             mock.patch("state._run_as_admin", return_value=subprocess.CompletedProcess([], 1)), \
             mock.patch("proxy.subprocess.Popen") as launch:
            proxy.apply_proxy({"type": "http", "host": "temporary", "port": 3128})
        launch.assert_not_called()
        record = json.loads(Path(proxy._proxy_recovery_path("Wi-Fi", "http")).read_text())
        self.assertEqual(record["protocols"]["http"]["host"], "old-http")
        self.assertEqual(record["protocols"]["https"]["host"], "old-https")
        commands = "\n".join(proxy._restore_proxy_commands(record))
        self.assertIn("-setwebproxy Wi-Fi old-http 8080 off", commands)
        self.assertIn("-setsecurewebproxy Wi-Fi old-https 8443 off", commands)
        self.assertIn("-setwebproxystate Wi-Fi on", commands)
        self.assertIn("-setsecurewebproxystate Wi-Fi off", commands)

    def test_failed_baseline_query_does_not_change_system_settings(self):
        with mock.patch("proxy.get_active_network_service", return_value="Wi-Fi"), \
             mock.patch("proxy.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="failed")), \
             mock.patch("state._run_as_admin") as admin:
            proxy.apply_proxy({"host": "temporary"})
        admin.assert_not_called()

    def test_successful_apply_schedules_independent_protocol_restoration(self):
        scripts = []
        captured = []
        try:
            def capture_job(args, **kwargs):
                wrapper = Path(args[-1])
                scripts.append(wrapper)
                text = wrapper.read_text()
                import re
                restore = re.search(r"bash ([^ '\"]*tm_proxy_restore_cmd_[^ '\"]+)", text)
                commands = Path(restore.group(1))
                scripts.append(commands)
                captured.append(commands.read_text())

            with mock.patch("proxy.get_active_network_service", return_value="Wi-Fi"), \
                 mock.patch("proxy.subprocess.run", side_effect=self._query), \
                 mock.patch("state._run_as_admin", return_value=subprocess.CompletedProcess([], 0)), \
                 mock.patch("proxy.subprocess.Popen", side_effect=capture_job) as launch:
                proxy.apply_proxy({"type": "http", "host": "temporary", "port": 3128})
            launch.assert_called_once()
            self.assertEqual(len(captured), 1)
            self.assertIn("old-http 8080 off", captured[0])
            self.assertIn("old-https 8443 off", captured[0])
            self.assertIn("-setsecurewebproxystate Wi-Fi off", captured[0])
        finally:
            for script in scripts:
                script.unlink(missing_ok=True)

    def test_authenticated_baseline_is_not_overwritten(self):
        output = "Enabled: Yes\nServer: private\nPort: 8080\nAuthenticated Proxy Enabled: 1\n"
        with mock.patch("proxy.get_active_network_service", return_value="Wi-Fi"), \
             mock.patch("proxy.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output)), \
             mock.patch("state._run_as_admin") as admin:
            proxy.apply_proxy({"type": "http", "host": "temporary"})
        admin.assert_not_called()

    def test_recovery_retains_record_until_every_command_succeeds(self):
        path = Path(self.tmp.name) / "recovery.json"
        record = {"service": "Wi-Fi", "proxy_type": "http", "protocols": {
            "http": {"host": "http-original", "port": "8080", "enabled": True},
            "https": {"host": "https-original", "port": "8443", "enabled": False}}}
        path.write_text(json.dumps(record))
        with mock.patch("state._run_as_admin", side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as admin:
            proxy._recover_stale_proxy(str(path))
            self.assertTrue(path.exists())
            proxy._recover_stale_proxy(str(path))
            self.assertFalse(path.exists())
        self.assertTrue(admin.call_args.args[0].startswith("set -e\n"))
        self.assertIn("https-original", admin.call_args.args[0])

    def test_legacy_recovery_record_still_restores(self):
        commands = proxy._restore_proxy_commands({"service": "Wi-Fi", "proxy_type": "socks5", "host": "original", "port": "1080", "enabled": True})
        self.assertIn("-setsocksfirewallproxy Wi-Fi original 1080 off", commands[0])
        self.assertIn("-setsocksfirewallproxystate Wi-Fi on", commands[1])

    def test_epoch_does_not_suppress_another_services_restore(self):
        self.assertNotEqual(proxy._channel_epoch_path("http", "Wi-Fi"),
                            proxy._channel_epoch_path("http", "Ethernet"))
        self.assertEqual(proxy._bump_channel_epoch("http", "Wi-Fi"), 1)
        self.assertEqual(proxy._bump_channel_epoch("http", "Ethernet"), 1)
        self.assertEqual(proxy._bump_channel_epoch("http", "Wi-Fi"), 2)


class LocalSafetyTests(unittest.TestCase):
    def test_ready_file_is_private_under_permissive_umask(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "ready.json"
            old = os.umask(0o022)
            try:
                server._write_ready_file(str(path), 8477, "test-token")
            finally:
                os.umask(old)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["port"], 8477)

    def test_signing_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            app = Path(root) / "Telegram.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_text("stub")
            with mock.patch("server.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 1, stderr=b"signature failure")]):
                self.assertFalse(server._patch_app_display_name(str(app), "Account"))

    def test_external_account_restore_preserves_original(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            external = root / "external"
            account = external / "account"
            tdata = account / "TelegramForcePortable/tdata"
            tdata.mkdir(parents=True)
            (tdata / "original").write_text("old session")
            data = root / "data"
            backup = data / "Backups/date/account"
            (backup / "tdata").mkdir(parents=True)
            (backup / "tdata/restored").write_text("backup session")
            with mock.patch.object(state, "DATA_DIR", str(data)), \
                 mock.patch.dict(state.config, {"extra_scan_dirs": [str(external)]}), \
                 mock.patch("server.find_telegram_pid", return_value=None):
                ok, message = backups.restore_backup(str(backup), str(account))
            self.assertTrue(ok, message)
            self.assertEqual((tdata / "restored").read_text(), "backup session")
            previous = list(tdata.parent.glob("tdata.bak.*"))
            self.assertEqual(len(previous), 1)
            self.assertEqual((previous[0] / "original").read_text(), "old session")


if __name__ == "__main__":
    unittest.main()
