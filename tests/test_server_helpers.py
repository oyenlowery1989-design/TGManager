import copy
import hashlib
import importlib
import json
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "TelegramManager.app" / "Contents" / "Resources"
sys.path.insert(0, str(RESOURCES_DIR))
os.environ.setdefault("TG_SESSION_TOKEN", "unit-test-token")

server = importlib.import_module("server")
state = importlib.import_module("state")
backups = importlib.import_module("backups")
proxy = importlib.import_module("proxy")

ACCOUNT_ID = "11111111-1111-4a11-8b11-111111111111"
LAUNCHER_FILE = RESOURCES_DIR / "launcher.swift"
CHROME_LAUNCHER_FILE = RESOURCES_DIR.parent / "MacOS" / "launcher_chrome.sh"


class ServerHelperTests(unittest.TestCase):
    def test_metadata_is_relocated_to_private_state(self):
        self.assertIn("Application Support/TelegramManager", server.METADATA_FILE)
        self.assertIn("Application Support/TelegramManager", server.WORKSPACES_FILE)
        self.assertTrue(server.METADATA_FILE.endswith("manager_data.json"))
        self.assertTrue(server.WORKSPACES_FILE.endswith("manager_workspaces.json"))

    def test_route_path_uses_session_token(self):
        self.assertEqual(server.ROUTE_PREFIX, "/unit-test-token")
        self.assertEqual(server._route_path("/unit-test-token/"), "/")
        self.assertEqual(server._route_path("/unit-test-token/api/accounts"), "/api/accounts")
        self.assertIsNone(server._route_path("/api/accounts"))

    def test_path_validation_blocks_traversal(self):
        safe_path = os.path.join(server.ROOT_DIR, "example-account")
        self.assertTrue(server.is_safe_path(safe_path))
        self.assertFalse(server.is_safe_path("/tmp/../etc/passwd"))
        self.assertFalse(server.is_safe_path("../outside"))


class NativeLauncherTests(unittest.TestCase):
    def test_navigation_policy_keeps_webview_loads_in_app(self):
        source = LAUNCHER_FILE.read_text()
        start = source.index("func webView(_ webView: WKWebView,")
        end = source.index("    // ── WKUIDelegate", start)
        policy = source[start:end]
        self.assertIn("decisionHandler(.allow)", policy)
        self.assertNotIn("decisionHandler(.cancel)", policy)


class BrowserFallbackLauncherTests(unittest.TestCase):
    def test_fallback_uses_an_authenticated_isolated_lifecycle(self):
        source = CHROME_LAUNCHER_FILE.read_text()
        self.assertIn('SESSION_TOKEN="${TG_SESSION_TOKEN:-}"', source)
        self.assertIn('URL="http://127.0.0.1:$PORT/$SESSION_TOKEN/"', source)
        self.assertIn('mktemp -d', source)
        self.assertIn('trap cleanup EXIT', source)
        self.assertNotIn('lsof -ti:$PORT', source)

    def test_obsolete_revert_command_is_not_shipped(self):
        self.assertFalse((RESOURCES_DIR / "Revert_to_Chrome.command").exists())

    def test_unused_pyobjc_launcher_is_not_shipped(self):
        self.assertFalse((RESOURCES_DIR / "app_window.py").exists())


class PersistedJsonTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_json_")
        self.original_config_file = state.CONFIG_FILE
        state.CONFIG_FILE = os.path.join(self.tmp, "manager_config.json")

    def tearDown(self):
        import shutil
        state.CONFIG_FILE = self.original_config_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_config_quarantines_valid_json_with_wrong_top_level_type(self):
        with open(state.CONFIG_FILE, "w") as f:
            json.dump([], f)

        config = state.load_config()

        self.assertEqual(config["port"], 8477)
        self.assertFalse(os.path.exists(state.CONFIG_FILE))
        self.assertEqual(len(list(Path(self.tmp).glob("manager_config.json.corrupt-*"))), 1)


class ImportTransactionTests(unittest.TestCase):
    def test_import_keeps_memory_unchanged_when_workspace_save_fails(self):
        original_meta = copy.deepcopy(state.metadata)
        original_config = copy.deepcopy(state.config)
        responses = []

        class Handler:
            def send_json(self, body):
                responses.append(body)

        payload = {"metadata": {"notes": {"/tmp/account": "new"}},
                   "config": {"port": 8478}, "workspaces": {}}
        try:
            with mock.patch("server.save_metadata"), mock.patch("server.save_config"), \
                 mock.patch("server.save_workspaces", side_effect=OSError("disk full")):
                server.RequestHandler._post_api_import_config(Handler(), payload)
            self.assertFalse(responses[-1]["success"])
            self.assertEqual(state.metadata, original_meta)
            self.assertEqual(state.config, original_config)
        finally:
            state.metadata.clear(); state.metadata.update(original_meta)
            state.config.clear(); state.config.update(original_config)


class ProxyRecoveryTests(unittest.TestCase):
    def test_proxy_recovery_paths_are_isolated_by_service_and_channel(self):
        paths = {
            proxy._proxy_recovery_path("Wi-Fi", "socks"),
            proxy._proxy_recovery_path("Wi-Fi", "http"),
            proxy._proxy_recovery_path("Ethernet", "socks"),
        }
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.startswith(os.path.join(state.DATA_DIR, "proxy_recovery") + os.sep)
                            for path in paths))


class ManagedAccountPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_account_path_")
        self.original_root = state.ROOT_DIR
        self.original_metadata_file = state.METADATA_FILE
        self.original_metadata = copy.deepcopy(state.metadata)
        state.ROOT_DIR = self.tmp
        state.METADATA_FILE = os.path.join(self.tmp, "manager_data.json")

    def tearDown(self):
        import shutil
        state.ROOT_DIR = self.original_root
        state.METADATA_FILE = self.original_metadata_file
        state.metadata.clear()
        state.metadata.update(self.original_metadata)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _account(self, name="account"):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, "TelegramForcePortable", "tdata"))
        return path

    def test_requires_a_real_account_with_tdata(self):
        account = self._account()
        self.assertTrue(state.is_managed_account_path(account))
        self.assertFalse(state.is_managed_account_path(os.path.join(self.tmp, "ordinary-folder")))

    def test_accepts_real_account_in_configured_extra_scan_dir(self):
        external = os.path.join(self.tmp, "external")
        account = os.path.join(external, "account")
        os.makedirs(os.path.join(account, "TelegramForcePortable", "tdata"))
        original = state.config.get("extra_scan_dirs")
        state.config["extra_scan_dirs"] = [external]
        try:
            self.assertTrue(state.is_managed_account_path(account))
        finally:
            state.config["extra_scan_dirs"] = original

    def test_ensure_account_id_is_stable_and_persisted(self):
        account = self._account()
        first = state.ensure_account_id(account)
        self.assertEqual(first, state.ensure_account_id(account))
        self.assertEqual(state.metadata["account_ids"][account], first)
        self.assertEqual(str(uuid.UUID(first)), first)

    def test_ensure_account_id_replaces_noncanonical_or_duplicate_state(self):
        account = self._account()
        other = self._account("other")
        state.metadata["account_ids"] = {other: ACCOUNT_ID, account: ACCOUNT_ID}

        replacement = state.ensure_account_id(account)

        self.assertEqual(state.metadata["account_ids"][other], ACCOUNT_ID)
        self.assertNotEqual(replacement, ACCOUNT_ID)
        self.assertEqual(str(uuid.UUID(replacement)), replacement)

        state.metadata["account_ids"] = {account: uuid.UUID(ACCOUNT_ID).hex}
        replacement = state.ensure_account_id(account)
        self.assertEqual(str(uuid.UUID(replacement)), replacement)
        self.assertNotEqual(replacement, uuid.UUID(ACCOUNT_ID).hex)

    def test_failed_account_id_persistence_does_not_change_memory(self):
        account = self._account()
        state.metadata.pop("account_ids", None)

        with mock.patch("state.save_metadata", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.ensure_account_id(account)

        self.assertNotIn(account, state.metadata.get("account_ids", {}))

    def test_rename_moves_account_id(self):
        old_path = self._account("old")
        state.metadata["account_ids"] = {old_path: ACCOUNT_ID}

        ok, new_path = server.rename_account(old_path, "new")

        self.assertTrue(ok)
        self.assertEqual(state.metadata["account_ids"], {new_path: ACCOUNT_ID})

    def test_rename_rewrites_workspace_account_paths(self):
        old_path = self._account("workspace-old")
        workspaces = {"Ops": {"accounts": [old_path], "icon": "📁", "created": ""}}
        saved = []

        with mock.patch("server.load_workspaces", return_value=copy.deepcopy(workspaces)), \
             mock.patch("server.save_workspaces", side_effect=lambda ws: saved.append(copy.deepcopy(ws))), \
             mock.patch("server.set_telegram_display_name"):
            ok, new_path = server.rename_account(old_path, "workspace-new")

        self.assertTrue(ok)
        self.assertEqual(saved[-1]["Ops"]["accounts"], [new_path])

    def test_delete_removes_account_id(self):
        account = self._account("delete-me")
        state.metadata["account_ids"] = {account: ACCOUNT_ID}

        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch("server.is_running", return_value=False), \
             mock.patch("server.backup_account", return_value=(True, "ok", "/backup")), \
             mock.patch("server.subprocess.run", return_value=completed):
            ok, _, _ = server.delete_account(account)

        self.assertTrue(ok)
        self.assertNotIn(account, state.metadata["account_ids"])

    def test_rejects_a_symlinked_account(self):
        account = self._account("real-account")
        alias = os.path.join(self.tmp, "account-alias")
        os.symlink(account, alias)
        self.assertFalse(state.is_managed_account_path(alias))

    def test_rename_refuses_a_non_account_folder(self):
        ordinary = os.path.join(self.tmp, "ordinary-folder")
        os.makedirs(ordinary)
        with mock.patch("server.is_running", return_value=False), \
             mock.patch("server.os.rename") as rename, \
             mock.patch("server.save_metadata"), \
             mock.patch("server.set_telegram_display_name"):
            ok, message = server.rename_account(ordinary, "renamed")
        self.assertFalse(ok)
        self.assertIn("valid account", message)
        rename.assert_not_called()

    def test_repair_refuses_a_symlinked_app_bundle(self):
        account = self._account()
        outside = os.path.join(self.tmp, "outside.app")
        os.makedirs(os.path.join(outside, "Contents", "MacOS"))
        with open(os.path.join(outside, "Contents", "MacOS", "Telegram"), "w") as f:
            f.write("external")
        os.symlink(outside, os.path.join(account, "Telegram.app"))
        with mock.patch("server.backup_account", return_value=(True, "ok", "backup")), \
             mock.patch("server.find_telegram_pid", return_value=None), \
             mock.patch("server.os.chmod") as chmod:
            results = server.repair_account(account, ["fix_perms"])
        self.assertFalse(results[-1]["ok"])
        self.assertIn("app bundle", results[-1]["msg"])
        chmod.assert_not_called()

    def test_repair_refuses_a_symlinked_app_binary(self):
        account = self._account("binary-link-account")
        app = os.path.join(account, "Telegram.app", "Contents", "MacOS")
        os.makedirs(app)
        outside = os.path.join(self.tmp, "external-telegram")
        with open(outside, "w") as f:
            f.write("external")
        os.symlink(outside, os.path.join(app, "Telegram"))
        with mock.patch("server.backup_account", return_value=(True, "ok", "backup")), \
             mock.patch("server.find_telegram_pid", return_value=None), \
             mock.patch("server.os.chmod") as chmod:
            results = server.repair_account(account, ["fix_perms"])
        self.assertFalse(results[-1]["ok"])
        self.assertIn("app bundle", results[-1]["msg"])
        chmod.assert_not_called()

    def test_clear_cache_skips_symlinked_cache_target(self):
        account = self._account("cache-link-account")
        tdata = os.path.join(account, "TelegramForcePortable", "tdata")
        outside = os.path.join(self.tmp, "outside-cache")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("must remain")
        os.symlink(outside, os.path.join(tdata, "emoji"))
        with mock.patch("server.find_telegram_pid", return_value=None), \
             mock.patch("server.subprocess.run") as run:
            run.return_value.returncode = 0
            server.clear_account_caches(account)
        removed = [call.args[0][-1] for call in run.call_args_list
                   if call.args and call.args[0] and call.args[0][0] == "rm"]
        self.assertNotIn(os.path.join(tdata, "emoji"), removed)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))

    def test_clear_cache_returns_busy_while_account_operation_is_locked(self):
        import threading
        account = self._account("busy-cache-account")
        lock = state._account_path_lock(account)
        lock.acquire()
        try:
            result = []
            thread = threading.Thread(target=lambda: result.append(
                server.clear_account_caches(account)))
            thread.start(); thread.join()
            self.assertEqual(result, [(False, 0)])
        finally:
            lock.release()

    def test_rename_returns_busy_while_account_operation_is_locked(self):
        import threading
        account = self._account("busy-rename-account")
        lock = state._account_path_lock(account)
        lock.acquire()
        try:
            result = []
            thread = threading.Thread(target=lambda: result.append(
                server.rename_account(account, "renamed")))
            thread.start(); thread.join()
            self.assertEqual(result, [(False, state._BUSY_MSG)])
        finally:
            lock.release()

    def test_setup_returns_busy_while_account_operation_is_locked(self):
        import threading
        account = self._account("busy-setup-account")
        lock = state._account_path_lock(account)
        lock.acquire()
        try:
            result = []
            thread = threading.Thread(target=lambda: result.append(server.setup_account(account)))
            thread.start(); thread.join()
            self.assertEqual(result, [(False, state._BUSY_MSG)])
        finally:
            lock.release()

    def test_delete_returns_busy_while_account_operation_is_locked(self):
        import threading
        account = self._account("busy-delete-account")
        lock = state._account_path_lock(account)
        lock.acquire()
        try:
            result = []
            thread = threading.Thread(target=lambda: result.append(server.delete_account(account)))
            thread.start(); thread.join()
            self.assertEqual(result, [(False, state._BUSY_MSG, "")])
        finally:
            lock.release()

    def test_shell_escaping_helpers(self):
        self.assertEqual(server._sq("a'b"), "'a'\\''b'")
        self.assertEqual(server._as_str("plain"), '"plain"')
        quoted = server._as_str('a"b')
        self.assertIn('(ASCII character 34)', quoted)
        self.assertTrue(quoted.startswith('"a"'))

    def test_validate_import_payload_accepts_good_export(self):
        payload = {
            "metadata": {
                "notes": {"/tmp/account": "note"},
                "usernames": {"/tmp/account": "alice"},
                "order": {"/tmp/account": 1},
                "colors": {"/tmp/account": "#ff0000"},
                "last_opened": {"/tmp/account": "2026-07-02T00:00:00"},
                "pinned": ["/tmp/account"],
                "proxies": {
                    "/tmp/account": {"type": "socks5", "host": "127.0.0.1", "port": 1080}
                },
                "dock_names": {"/tmp/account": "Alice"},
                "account_ids": {"/tmp/account": ACCOUNT_ID},
            },
            "config": copy.deepcopy(server.DEFAULT_CONFIG),
            "workspaces": {
                "Ops": {
                    "accounts": ["/tmp/account"],
                    "icon": "📁",
                    "created": "2026-07-02T00:00:00",
                }
            },
        }
        payload["config"]["port"] = 8477
        ok, message, normalized = server._validate_import_payload(payload)
        self.assertTrue(ok, message)
        self.assertEqual(normalized["config"]["port"], 8477)
        self.assertEqual(normalized["metadata"]["notes"]["/tmp/account"], "note")
        self.assertEqual(normalized["metadata"]["account_ids"]["/tmp/account"], ACCOUNT_ID)
        self.assertEqual(normalized["workspaces"]["Ops"]["accounts"], ["/tmp/account"])

    def test_validate_import_payload_rejects_bad_types(self):
        payload = {
            "metadata": {"notes": ["not", "a", "dict"]},
            "config": {"port": "8477"},
            "workspaces": {},
        }
        ok, message, normalized = server._validate_import_payload(payload)
        self.assertFalse(ok)
        self.assertIsNone(normalized)
        self.assertTrue(message)

    def test_import_rejects_invalid_account_id_value(self):
        for value in (12, "", uuid.UUID(ACCOUNT_ID).hex, ACCOUNT_ID.upper()):
            payload = {
                "metadata": {"account_ids": {"/tmp/account": value}},
                "config": {},
                "workspaces": {},
            }
            with self.subTest(value=value):
                self.assertFalse(server._validate_import_payload(payload)[0])

    def test_import_rejects_duplicate_account_ids(self):
        payload = {
            "metadata": {"account_ids": {
                "/tmp/first": ACCOUNT_ID,
                "/tmp/second": ACCOUNT_ID,
            }},
            "config": {},
            "workspaces": {},
        }

        self.assertFalse(server._validate_import_payload(payload)[0])


class ReadyFileTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_ready_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ready_file_is_atomic_and_contains_server_identity(self):
        ready = os.path.join(self.tmp, "ready.json")
        server._write_ready_file(ready, 8477, "test-token")
        with open(ready, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {
                "pid": os.getpid(), "port": 8477, "session_token": "test-token",
            })
        self.assertFalse(os.path.exists(ready + ".tmp"))

    def test_ready_file_cleanup_ignores_missing_path(self):
        server._remove_ready_file(os.path.join(self.tmp, "missing.json"))


class TelegramUpdateLifecycleTests(unittest.TestCase):
    """Regression coverage for the shared-master / disposable-clone lifecycle."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_update_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_bundle(self, parent, version="7.1.3"):
        app = os.path.join(parent, "Telegram.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        os.makedirs(os.path.join(app, "Contents", "_CodeSignature"))
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            import plistlib
            plistlib.dump({"CFBundleIdentifier": server.TDESKTOP_BUNDLE_ID,
                           "CFBundleShortVersionString": version}, f)
        with open(os.path.join(app, "Contents", "MacOS", "Telegram"), "wb") as f:
            f.write(b"test executable")
        with open(os.path.join(app, "Contents", "_CodeSignature", "CodeResources"), "wb") as f:
            f.write(b"test resources")
        return app

    def test_clone_stamp_tracks_master_fingerprint(self):
        master = self._make_bundle(self.tmp)
        account = os.path.join(self.tmp, "account")
        os.makedirs(account)
        fingerprint = server._bundle_fingerprint(master)
        self.assertIsNotNone(fingerprint)
        server._write_clone_stamp(account, fingerprint)
        self.assertEqual(server._read_clone_stamp(account), fingerprint)

    def test_staged_update_version_reads_telegram_marker(self):
        account = os.path.join(self.tmp, "account")
        marker_dir = os.path.join(account, "TelegramForcePortable", "tupdates", "temp", "tdata")
        os.makedirs(marker_dir)
        with open(os.path.join(os.path.dirname(marker_dir), "ready"), "wb") as f:
            f.write(b"1")
        with open(os.path.join(marker_dir, "version"), "wb") as f:
            f.write(b"header00" + "7.1.3".encode("utf-32-le"))
        self.assertEqual(server._staged_update_version(account), "7.1.3")

    def test_update_never_falls_back_to_an_account_clone(self):
        with mock.patch("server.scan_accounts", return_value=[]):
            ok, message = server.update_all_apps()
        self.assertFalse(ok)
        self.assertIn("Choose a freshly downloaded", message)

    def test_update_rejects_an_explicit_account_clone(self):
        account = os.path.join(self.tmp, "account")
        os.makedirs(account)
        clone = self._make_bundle(account)
        accounts = [{"path": account, "name": "account", "running": False}]
        with mock.patch("server.scan_accounts", return_value=accounts), \
             mock.patch("server.is_allowed_app_source", return_value=True):
            ok, message = server.update_all_apps(clone)
        self.assertFalse(ok)
        self.assertIn("account-local clone", message)

    def test_shared_app_setup_refuses_to_replace_master_while_an_account_runs(self):
        source = self._make_bundle(self.tmp)
        account = {"path": os.path.join(self.tmp, "account"), "name": "account", "running": True}
        response = {}

        class Handler:
            def send_json(self, payload):
                response.update(payload)

        with mock.patch("server.is_allowed_app_source", return_value=True), \
             mock.patch("server._replace_shared_app", return_value=(True, "7.1.3")), \
             mock.patch("server.scan_accounts", return_value=[account]):
            server.RequestHandler._post_api_shared_app_setup(Handler(), {"source": source})

        self.assertFalse(response["success"])
        self.assertIn("Close Telegram first", response["message"])

    def test_shared_app_setup_requires_a_selected_source(self):
        source = self._make_bundle(self.tmp)
        response = {}

        class Handler:
            def send_json(self, payload):
                response.update(payload)

        previous = server.config.get("app_source")
        server.config["app_source"] = source
        try:
            with mock.patch("server.is_allowed_app_source", return_value=True), \
                 mock.patch("server._replace_shared_app", return_value=(True, "7.1.3")), \
                 mock.patch("server.scan_accounts", return_value=[]):
                server.RequestHandler._post_api_shared_app_setup(Handler(), {})
        finally:
            server.config["app_source"] = previous

        self.assertFalse(response["success"])
        self.assertIn("Choose a freshly downloaded", response["message"])

    def test_shared_app_setup_rejects_an_account_clone(self):
        account_path = os.path.join(self.tmp, "account")
        os.makedirs(account_path)
        clone = self._make_bundle(account_path)
        response = {}

        class Handler:
            def send_json(self, payload):
                response.update(payload)

        account = {"path": account_path, "name": "account", "running": False}
        with mock.patch("server.is_allowed_app_source", return_value=True), \
             mock.patch("server._replace_shared_app", return_value=(True, "7.1.3")), \
             mock.patch("server.scan_accounts", return_value=[account]):
            server.RequestHandler._post_api_shared_app_setup(Handler(), {"source": clone})

        self.assertFalse(response["success"])
        self.assertIn("account-local clone", response["message"])

    def test_replace_shared_app_copies_then_verifies_the_new_master(self):
        source_parent = os.path.join(self.tmp, "source")
        shared_parent = os.path.join(self.tmp, "shared", "macOS")
        os.makedirs(source_parent)
        os.makedirs(shared_parent)
        source = self._make_bundle(source_parent, "7.2.0")
        old_master = self._make_bundle(shared_parent, "7.1.3")
        original_shared = server.SHARED_MACOS_APP
        original_apps_dir = server.SHARED_APPS_DIR
        server.SHARED_MACOS_APP = old_master
        server.SHARED_APPS_DIR = os.path.dirname(shared_parent)
        checked = []
        original_run = server.subprocess.run

        def run(command, *args, **kwargs):
            if command[0] == "xattr":
                return mock.Mock(returncode=0)
            return original_run(command, *args, **kwargs)

        try:
            with mock.patch("server._verify_tdesktop_bundle", side_effect=lambda path: (checked.append(path) or (True, ""))), \
                 mock.patch("server.subprocess.run", side_effect=run):
                ok, version = server._replace_shared_app(source)
        finally:
            server.SHARED_MACOS_APP = original_shared
            server.SHARED_APPS_DIR = original_apps_dir

        self.assertTrue(ok)
        self.assertEqual(version, "7.2.0")
        self.assertEqual(checked, [source, old_master + ".new"])
        self.assertEqual(server._bundle_version(old_master), "7.2.0")
        self.assertEqual(server._bundle_version(old_master + ".previous"), "7.1.3")

    def test_update_blocks_running_accounts_before_replacing_the_master(self):
        account = {"path": os.path.join(self.tmp, "account"), "name": "account", "running": True}
        with mock.patch("server.scan_accounts", return_value=[account]), \
             mock.patch("server._replace_shared_app", return_value=(True, "7.1.3")) as replace:
            ok, message = server.update_all_apps("/chosen/Telegram.app")

        self.assertFalse(ok)
        self.assertIn("Close Telegram first", message)
        replace.assert_not_called()

    def test_open_replaces_a_fingerprint_mismatched_clone_without_touching_tdata(self):
        master_parent = os.path.join(self.tmp, "master")
        account = os.path.join(self.tmp, "account")
        os.makedirs(master_parent)
        os.makedirs(account)
        master = self._make_bundle(master_parent, "7.2.0")
        clone = self._make_bundle(account, "7.1.3")
        tdata = os.path.join(account, "TelegramForcePortable", "tdata")
        os.makedirs(tdata)
        session_file = os.path.join(tdata, "session")
        with open(session_file, "wb") as f:
            f.write(b"keep this session")
        server._write_clone_stamp(account, "not-the-master")
        original_run = server.subprocess.run

        def run(command, *args, **kwargs):
            if command[0] == "xattr":
                return mock.Mock(returncode=0)
            if command[0] == "open":
                return mock.Mock(returncode=0, stderr=b"")
            return original_run(command, *args, **kwargs)

        with mock.patch("server.get_shared_app", return_value=master), \
             mock.patch("server.is_running", return_value=False), \
             mock.patch("server.subprocess.run", side_effect=run), \
             mock.patch("server.save_metadata"), \
             mock.patch("server._watcher_exempt"), \
             mock.patch("server.invalidate_tdata_size"):
            ok, _ = server.open_account(account)

        self.assertTrue(ok)
        with open(session_file, "rb") as f:
            self.assertEqual(f.read(), b"keep this session")
        self.assertEqual(server._bundle_version(server.find_account_app(account)), "7.2.0")
        self.assertEqual(server._read_clone_stamp(account), server._bundle_fingerprint(master))

    def test_stale_updater_state_is_archived_not_deleted(self):
        account = os.path.join(self.tmp, "account")
        marker_dir = os.path.join(account, "TelegramForcePortable", "tupdates", "temp", "tdata")
        os.makedirs(marker_dir)
        with open(os.path.join(os.path.dirname(marker_dir), "ready"), "wb") as f:
            f.write(b"1")
        with open(os.path.join(marker_dir, "version"), "wb") as f:
            f.write(b"header00" + "7.0.7".encode("utf-32-le"))
        original_shared_apps = server.SHARED_APPS_DIR
        server.SHARED_APPS_DIR = os.path.join(self.tmp, "shared-apps")
        try:
            archived, skipped = server._archive_stale_updates(
                [{"path": account, "name": "account"}], "7.1.3"
            )
        finally:
            server.SHARED_APPS_DIR = original_shared_apps
        self.assertEqual((archived, skipped), (1, 0))
        self.assertFalse(os.path.exists(os.path.join(account, "TelegramForcePortable", "tupdates")))
        recovery = os.path.join(self.tmp, "shared-apps", "update-recovery")
        self.assertTrue(any("ready" in files for _, _, files in os.walk(recovery)))

    def test_version_comparison_handles_short_versions(self):
        self.assertEqual(server._version_tuple("7.1"), server._version_tuple("7.1.0"))
        self.assertLess(server._version_tuple("7.0.7"), server._version_tuple("7.1.3"))


class BackupPathTests(unittest.TestCase):
    """Backup guard + crash-safety behaviors against a throwaway DATA_DIR."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self._orig_root = state.ROOT_DIR
        self._orig_metadata_file = state.METADATA_FILE
        self._orig_metadata = copy.deepcopy(state.metadata)
        self._orig_backup_map_cache = copy.deepcopy(backups._backup_map_cache)
        self.tmp = tempfile.mkdtemp(prefix="tm_test_")
        state.DATA_DIR = self.tmp
        state.ROOT_DIR = self.tmp
        state.METADATA_FILE = os.path.join(self.tmp, "manager_data.json")
        self.account = os.path.join(self.tmp, "account")
        os.makedirs(os.path.join(self.account, "TelegramForcePortable", "tdata"))

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        state.ROOT_DIR = self._orig_root
        state.METADATA_FILE = self._orig_metadata_file
        state.metadata.clear()
        state.metadata.update(self._orig_metadata)
        backups._backup_map_cache.clear()
        backups._backup_map_cache.update(self._orig_backup_map_cache)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct", account_id=None, account_name=None):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        if account_id is not None:
            with open(os.path.join(b, "backup.json"), "w", encoding="utf-8") as f:
                json.dump({"account_id": account_id, "account_name": account_name,
                           "created_at": "2026-01-01T00:00:00"}, f)
        return b

    def test_resolve_backup_dir_accepts_two_level_paths(self):
        b = self._make_backup()
        self.assertEqual(server._resolve_backup_dir(b), os.path.realpath(b))

    def test_resolve_backup_dir_rejects_everything_else(self):
        self._make_backup()
        for bad in (
            os.path.join(self.tmp, "Backups"),                      # root
            os.path.join(self.tmp, "Backups", "2026-01-01_00-00"),  # 1 level
            os.path.join(self.tmp, "some-live-account"),            # outside
            "/etc/passwd",
            "",
            None,
        ):
            self.assertIsNone(server._resolve_backup_dir(bad), bad)

    def test_list_backups_skips_partial_dirs(self):
        self._make_backup(account="good")
        crashed = os.path.join(self.tmp, "Backups", "2026-01-01_00-00", "crashed.partial")
        os.makedirs(os.path.join(crashed, "tdata"))
        names = [b["account"] for b in server.list_backups()]
        self.assertEqual(names, ["good"])

    def test_list_backups_uses_manifest_identity_with_legacy_fallback(self):
        manifest = self._make_backup(account=ACCOUNT_ID + "-2", account_id=ACCOUNT_ID,
                                     account_name="Visible account")
        legacy = self._make_backup(account="legacy")
        listed = {b["backup_path"]: b for b in backups.list_backups()}
        self.assertEqual(listed[manifest]["account"], "Visible account")
        self.assertEqual(listed[manifest]["account_name"], "Visible account")
        self.assertEqual(listed[manifest]["account_id"], ACCOUNT_ID)
        self.assertEqual(listed[legacy]["account"], "legacy")
        self.assertEqual(listed[legacy]["account_name"], "legacy")
        self.assertIsNone(listed[legacy]["account_id"])
        self.assertEqual(backups._last_backup_map()[ACCOUNT_ID], "2026-01-01_00-00")

    def test_malformed_manifests_fall_back_to_legacy(self):
        manifests = (
            {"account_id": uuid.UUID(ACCOUNT_ID).hex, "account_name": "Account",
             "created_at": "2026-01-01T00:00:00"},
            {"account_id": ACCOUNT_ID, "account_name": "",
             "created_at": "2026-01-01T00:00:00"},
            {"account_id": ACCOUNT_ID, "account_name": "Account", "created_at": 1},
        )
        for index, manifest in enumerate(manifests):
            stored_name = f"legacy-{index}"
            path = self._make_backup(date=f"2026-01-0{index + 1}_00-00", account=stored_name)
            with open(os.path.join(path, "backup.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            listed = {b["backup_path"]: b for b in backups.list_backups()}[path]
            with self.subTest(manifest=manifest):
                self.assertEqual(listed["account"], stored_name)
                self.assertIsNone(listed["account_id"])

    def test_last_backup_status_follows_account_id_after_rename(self):
        self._make_backup(account=ACCOUNT_ID, account_id=ACCOUNT_ID, account_name="Old name")
        state.metadata["account_ids"] = {self.account: ACCOUNT_ID}
        backups._backup_map_cache["ts"] = 0.0
        handler = mock.Mock()

        with mock.patch("server.scan_accounts_cached", return_value=[{
                "path": self.account, "name": "Renamed", "group": "Root"}]):
            server.RequestHandler._get_api_accounts(handler)

        accounts = handler.send_json.call_args.args[0]
        self.assertEqual(accounts[0]["last_backup"], "2026-01-01_00-00")

    def test_accounts_endpoint_ignores_malformed_account_id_map(self):
        self._make_backup(account="Account")
        state.metadata["account_ids"] = []
        backups._backup_map_cache["ts"] = 0.0
        handler = mock.Mock()

        with mock.patch("server.scan_accounts_cached", return_value=[{
                "path": self.account, "name": "Account", "group": "Root"}]):
            server.RequestHandler._get_api_accounts(handler)

        accounts = handler.send_json.call_args.args[0]
        self.assertEqual(accounts[0]["last_backup"], "2026-01-01_00-00")

    def test_same_second_backups_get_distinct_destinations(self):
        first = backups.backup_account(self.account, "Account")
        second = backups.backup_account(self.account, "Account")
        self.assertTrue(first[0]); self.assertTrue(second[0])
        self.assertNotEqual(first[2], second[2])
        self.assertIn(os.path.relpath(second[2], state.DATA_DIR), second[1])
        with open(os.path.join(first[2], "backup.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        self.assertEqual(manifest["account_id"], state.metadata["account_ids"][self.account])
        self.assertEqual(manifest["account_name"], "Account")
        self.assertTrue(manifest["created_at"])

    def test_backup_rejects_destination_outside_backups_root(self):
        with mock.patch("backups.state.ensure_account_id", return_value="../../escape"), \
             mock.patch("backups._copy_tdata_excluding_cache", return_value=(True, "")):
            ok, _, _ = backups.backup_account(self.account, "Account")

        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))

    def test_failed_second_backup_keeps_existing_completed_backup(self):
        first = backups.backup_account(self.account, "Account")
        with mock.patch("backups.os.rename", side_effect=OSError("finalize failed")):
            second = backups.backup_account(self.account, "Account")
        self.assertTrue(first[0]); self.assertFalse(second[0])
        self.assertTrue(os.path.isdir(first[2]))

    def test_restore_rejects_non_backup_source(self):
        live = os.path.join(self.tmp, "live-account")
        os.makedirs(os.path.join(live, "TelegramForcePortable", "tdata"))
        ok, msg = server.restore_backup(live, live)
        self.assertFalse(ok)
        self.assertIn("Invalid backup path", msg)

    def test_restore_rejects_symlinked_destination(self):
        import tempfile
        backup = self._make_backup()
        acct = os.path.join(self.tmp, "acct-folder")
        os.makedirs(acct)
        outside = tempfile.mkdtemp(prefix="tm_outside_")
        try:
            # TelegramForcePortable is a symlink escaping the managed tree
            os.symlink(outside, os.path.join(acct, "TelegramForcePortable"))
            ok, msg = server.restore_backup(backup, acct)
            self.assertFalse(ok)
            self.assertIn("not a valid account folder", msg)
            self.assertFalse(os.path.isdir(os.path.join(outside, "tdata")))
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)


class LockEnforcementTests(unittest.TestCase):
    """Server-side password-lock enforcement (state.config-driven)."""

    def setUp(self):
        self._orig_hash = state.config.get("lock_password_hash")
        self._orig_salt = state.config.get("lock_password_salt")
        self._orig_timeout = state.config.get("lock_timeout_minutes")
        state.config["lock_password_hash"] = None
        state.config["lock_password_salt"] = None
        state.config["lock_timeout_minutes"] = 5
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def tearDown(self):
        state.config["lock_password_hash"] = self._orig_hash
        state.config["lock_password_salt"] = self._orig_salt
        state.config["lock_timeout_minutes"] = self._orig_timeout
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def _set_password(self, password="correct-password", salt="testsalt"):
        state.config["lock_password_salt"] = salt
        state.config["lock_password_hash"] = hashlib.sha256((salt + password).encode()).hexdigest()

    def test_lock_enabled_reflects_config(self):
        self.assertFalse(server._lock_enabled())
        self._set_password()
        self.assertTrue(server._lock_enabled())

    def test_verify_lock_password(self):
        self._set_password()
        self.assertTrue(server._verify_lock_password("correct-password"))
        self.assertFalse(server._verify_lock_password("wrong-password"))

    def test_verify_lock_password_false_when_lock_disabled(self):
        self.assertFalse(server._verify_lock_password("anything"))

    def test_server_unlock_and_lock(self):
        self._set_password()
        server._unlock_fail_count = 3
        server._server_unlock()
        self.assertNotEqual(server._lock_unlocked_at, 0.0)
        self.assertNotEqual(server._lock_last_activity, 0.0)
        self.assertEqual(server._unlock_fail_count, 0)
        server._server_lock()
        self.assertEqual(server._lock_unlocked_at, 0.0)

    def test_register_unlock_failure_increments(self):
        self.assertEqual(server._register_unlock_failure(), 1)
        self.assertEqual(server._register_unlock_failure(), 2)
        self.assertEqual(server._register_unlock_failure(), 3)

    def test_check_and_touch_unlocked_true_when_lock_disabled(self):
        state.config["lock_password_hash"] = None
        self.assertEqual(server._lock_unlocked_at, 0.0)
        self.assertTrue(server._check_and_touch_unlocked())

    def test_check_and_touch_unlocked_false_when_never_unlocked(self):
        self._set_password()
        self.assertFalse(server._check_and_touch_unlocked())

    def test_check_and_touch_unlocked_true_after_unlock(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 5
        server._server_unlock()
        self.assertTrue(server._check_and_touch_unlocked())
        first = server._lock_last_activity
        time.sleep(0.01)
        self.assertTrue(server._check_and_touch_unlocked())
        self.assertGreater(server._lock_last_activity, first)

    def test_check_and_touch_unlocked_expires_and_relocks(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 60
        server._server_unlock()
        server._lock_last_activity -= (61 * 60)
        self.assertFalse(server._check_and_touch_unlocked())
        self.assertEqual(server._lock_unlocked_at, 0.0)

    def test_check_and_touch_unlocked_never_expires_when_timeout_zero(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 0
        server._server_unlock()
        server._lock_last_activity -= (999 * 60)
        self.assertTrue(server._check_and_touch_unlocked())

    def test_is_unlocked_no_touch_does_not_mutate_activity(self):
        self._set_password()
        state.config["lock_timeout_minutes"] = 60
        server._server_unlock()
        server._lock_last_activity -= (30 * 60)   # within timeout, not past it
        before = server._lock_last_activity
        self.assertTrue(server._is_unlocked_no_touch())
        self.assertEqual(server._lock_last_activity, before)
        self.assertTrue(server._is_unlocked_no_touch())
        self.assertEqual(server._lock_last_activity, before)


class BackupDeleteTests(unittest.TestCase):
    """delete_backup()'s 2-level guard and cleanup behavior."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_test_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        return b

    def test_delete_backup_removes_dir_and_empty_parent(self):
        b = self._make_backup()
        ok, msg = backups.delete_backup(b)
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.isdir(b))
        self.assertFalse(os.path.isdir(os.path.dirname(b)))

    def test_delete_backup_rejects_invalid_path(self):
        b = self._make_backup()
        for bad in (
            os.path.join(self.tmp, "Backups"),
            os.path.join(self.tmp, "outside"),
        ):
            ok, msg = backups.delete_backup(bad)
            self.assertFalse(ok)
            self.assertEqual(msg, "Invalid backup path")
        self.assertTrue(os.path.isdir(b))


class CheckHealthTests(unittest.TestCase):
    """check_health()'s session-freshness + integrity heuristics."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_health_")
        self.tdata = os.path.join(self.tmp, "tdata")
        os.makedirs(self.tdata)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_key_datas(self, age_seconds=0):
        path = os.path.join(self.tdata, "key_datas")
        with open(path, "w") as f:
            f.write("x")
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))

    def test_fresh_session(self):
        self._touch_key_datas(age_seconds=0)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "fresh")
        self.assertEqual(result["status"], "ok")

    def test_stale_session(self):
        self._touch_key_datas(age_seconds=70 * 86400)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "stale")
        self.assertEqual(result["status"], "warn")

    def test_expired_session_is_still_warn_not_error(self):
        self._touch_key_datas(age_seconds=200 * 86400)
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["expiry"], "expired")
        self.assertEqual(result["status"], "warn")

    def test_missing_tdata(self):
        result = server.check_health(self.tmp, True, False, self.tdata)
        self.assertEqual(result["status"], "error")
        self.assertIn("tdata missing", result["issues"])

    def test_empty_tdata(self):
        result = server.check_health(self.tmp, True, True, self.tdata)
        self.assertEqual(result["status"], "error")
        self.assertIn("tdata is empty", result["issues"])


class RestoreBackupRollbackTests(unittest.TestCase):
    """restore_backup()'s crash-safety rollback when the second rename fails."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self._orig_root = state.ROOT_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_restore_")
        state.DATA_DIR = self.tmp
        state.ROOT_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        state.ROOT_DIR = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        with open(os.path.join(b, "tdata", "backup_marker"), "w") as f:
            f.write("backup")
        return b

    def test_restore_rolls_back_original_tdata_on_second_rename_failure(self):
        backup = self._make_backup()
        acct = os.path.join(self.tmp, "live-account")
        tdata_dst = os.path.join(acct, "TelegramForcePortable", "tdata")
        os.makedirs(tdata_dst)
        with open(os.path.join(tdata_dst, "original_marker"), "w") as f:
            f.write("original")

        real_rename = os.rename
        call_count = {"n": 0}

        def flaky_rename(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated failure")
            return real_rename(*args, **kwargs)

        with mock.patch("backups.os.rename", side_effect=flaky_rename):
            ok, msg = backups.restore_backup(backup, acct)

        self.assertFalse(ok)
        self.assertIn("original tdata has been restored", msg)
        self.assertTrue(os.path.isfile(os.path.join(tdata_dst, "original_marker")))
        self.assertFalse(os.path.isdir(tdata_dst + ".new"))
        bak_dirs = [d for d in os.listdir(os.path.dirname(tdata_dst))
                    if d.startswith("tdata.bak.")]
        self.assertEqual(bak_dirs, [])


class PruneBackupsTests(unittest.TestCase):
    """prune_backups()'s keep-N-per-account enforcement."""

    def setUp(self):
        import tempfile
        self._orig_data = state.DATA_DIR
        self._orig_keep = state.config.get("backup_keep_per_account")
        self.tmp = tempfile.mkdtemp(prefix="tm_prune_")
        state.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        state.DATA_DIR = self._orig_data
        state.config["backup_keep_per_account"] = self._orig_keep
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date, account, account_id=None, account_name=None):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
        if account_id is not None:
            with open(os.path.join(b, "backup.json"), "w", encoding="utf-8") as f:
                json.dump({"account_id": account_id, "account_name": account_name,
                           "created_at": date}, f)
        return b

    def test_prune_keeps_only_newest_n(self):
        account_id = ACCOUNT_ID
        dates = ["2026-01-01_00-00", "2026-01-02_00-00", "2026-01-03_00-00", "2026-01-04_00-00"]
        for suffix, d in enumerate(dates, 1):
            self._make_backup(d, f"{account_id}-{suffix}", account_id, "acct")
        state.config["backup_keep_per_account"] = 2
        backups.prune_backups(account_id)
        remaining = sorted(b["date"] for b in backups.list_backups() if b["account_id"] == account_id)
        self.assertEqual(remaining, ["2026-01-03_00-00", "2026-01-04_00-00"])

    def test_prune_keeps_newest_same_second_collision(self):
        for account in (ACCOUNT_ID, ACCOUNT_ID + "-2", ACCOUNT_ID + "-3"):
            self._make_backup("2026-01-01_00-00-00", account, ACCOUNT_ID, "acct")
        state.config["backup_keep_per_account"] = 1

        backups.prune_backups(ACCOUNT_ID)

        remaining = [os.path.basename(b["backup_path"]) for b in backups.list_backups()
                     if b["account_id"] == ACCOUNT_ID]
        self.assertEqual(remaining, [ACCOUNT_ID + "-3"])

    def test_prune_never_removes_manifest_free_legacy_backups(self):
        legacy = [self._make_backup(date, "acct") for date in (
            "2026-01-01_00-00", "2026-01-02_00-00")]
        state.config["backup_keep_per_account"] = 1

        backups.prune_backups(ACCOUNT_ID)

        self.assertTrue(all(os.path.isdir(path) for path in legacy))

    def test_prune_is_noop_when_keep_is_zero(self):
        dates = ["2026-01-01_00-00", "2026-01-02_00-00", "2026-01-03_00-00"]
        for d in dates:
            self._make_backup(d, "acct")
        state.config["backup_keep_per_account"] = 0
        backups.prune_backups("acct")
        remaining = sorted(b["date"] for b in backups.list_backups() if b["account"] == "acct")
        self.assertEqual(remaining, dates)


class CreateAccountValidationTests(unittest.TestCase):
    """create_account()'s early-return name-validation paths."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tm_create_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_empty_name(self):
        ok, msg = server.create_account("   ", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, "Account name cannot be empty")

    def test_rejects_bad_characters(self):
        ok, msg = server.create_account("bad/name", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertIn("invalid characters", msg)

    def test_rejects_dot_names(self):
        for name in (".", "..", ".hidden"):
            ok, msg = server.create_account(name, self.tmp, open_after=False)
            self.assertFalse(ok, name)
            self.assertEqual(msg, "Account name cannot start with a dot")

    def test_rejects_reserved_names(self):
        ok, msg = server.create_account("Backups", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, '"Backups" is a reserved folder name')

    def test_rejects_duplicate_name(self):
        os.makedirs(os.path.join(self.tmp, "Existing"))
        ok, msg = server.create_account("Existing", self.tmp, open_after=False)
        self.assertFalse(ok)
        self.assertEqual(msg, 'A folder named "Existing" already exists in that location')


if __name__ == "__main__":
    unittest.main()
