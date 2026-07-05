import copy
import importlib
import os
import sys
import unittest
from pathlib import Path

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "TelegramManager.app" / "Contents" / "Resources"
sys.path.insert(0, str(RESOURCES_DIR))
os.environ.setdefault("TG_SESSION_TOKEN", "unit-test-token")

server = importlib.import_module("server")


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


class BackupPathTests(unittest.TestCase):
    """Backup guard + crash-safety behaviors against a throwaway DATA_DIR."""

    def setUp(self):
        import tempfile
        self._orig_data = server.DATA_DIR
        self.tmp = tempfile.mkdtemp(prefix="tm_test_")
        server.DATA_DIR = self.tmp

    def tearDown(self):
        import shutil
        server.DATA_DIR = self._orig_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, date="2026-01-01_00-00", account="acct"):
        b = os.path.join(self.tmp, "Backups", date, account)
        os.makedirs(os.path.join(b, "tdata"))
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


if __name__ == "__main__":
    unittest.main()
