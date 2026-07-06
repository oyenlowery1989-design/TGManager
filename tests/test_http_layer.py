"""HTTP-layer integration tests: spin up the real ThreadedHTTPServer on an
ephemeral port and drive it with http.client, instead of calling server.py's
internal functions directly (as tests/test_server_helpers.py does). This
covers the routing/dispatch layer itself — the session-token URL prefix, the
lock gate, and the Origin check on POST — none of which any existing test
exercises end-to-end.
"""

import copy
import http.client
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "TelegramManager.app" / "Contents" / "Resources"
sys.path.insert(0, str(RESOURCES_DIR))
os.environ.setdefault("TG_SESSION_TOKEN", "unit-test-token")

server = importlib.import_module("server")
state = importlib.import_module("state")


class HTTPLayerTests(unittest.TestCase):
    """One real server bound to 127.0.0.1:0 (ephemeral port), shared read-only
    across tests — no test here mutates accounts/config, only lock state
    (reset in setUp/tearDown, same pattern as LockEnforcementTests)."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        cls.port = cls.httpd.server_address[1]
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # The real manager_config.json on this machine may have the password
        # lock configured — force it off by default so non-lock tests see a
        # deterministic unlocked server; lock-specific tests opt back in.
        self._orig_hash = state.config.get("lock_password_hash")
        self._orig_salt = state.config.get("lock_password_salt")
        state.config["lock_password_hash"] = None
        state.config["lock_password_salt"] = None
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def tearDown(self):
        state.config["lock_password_hash"] = self._orig_hash
        state.config["lock_password_salt"] = self._orig_salt
        server._lock_unlocked_at = 0.0
        server._lock_last_activity = 0.0
        server._unlock_fail_count = 0

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _get(self, path, headers=None):
        conn = self._conn()
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def _post(self, path, body=None, headers=None):
        conn = self._conn()
        try:
            data = json.dumps(body or {}).encode()
            hdrs = {"Content-Type": "application/json", "Content-Length": str(len(data))}
            hdrs.update(headers or {})
            conn.request("POST", path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    # ── Routing ──────────────────────────────────────────────────────────

    def test_missing_session_token_prefix_returns_404(self):
        status, _ = self._get("/api/accounts")
        self.assertEqual(status, 404)

    def test_unknown_route_returns_404(self):
        status, _ = self._get(server.ROUTE_PREFIX + "/api/does-not-exist")
        self.assertEqual(status, 404)

    def test_get_accounts_returns_200_json_array(self):
        status, body = self._get(server.ROUTE_PREFIX + "/api/accounts")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)

    # ── Lock gate ────────────────────────────────────────────────────────

    def test_lock_status_is_reachable_while_locked(self):
        # /api/lock-status is in _LOCK_EXEMPT — must never 423 itself, or the
        # lock screen could never poll its own state.
        state.config["lock_password_salt"] = "s"
        state.config["lock_password_hash"] = "h"
        status, body = self._get(server.ROUTE_PREFIX + "/api/lock-status")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["enabled"])

    def test_locked_account_endpoint_returns_423(self):
        state.config["lock_password_salt"] = "s"
        state.config["lock_password_hash"] = "h"
        status, body = self._get(server.ROUTE_PREFIX + "/api/accounts")
        self.assertEqual(status, 423)
        self.assertTrue(json.loads(body)["locked"])

    def test_unlocked_account_endpoint_returns_200(self):
        state.config["lock_password_salt"] = "s"
        state.config["lock_password_hash"] = "h"
        server._server_unlock()
        status, _ = self._get(server.ROUTE_PREFIX + "/api/accounts")
        self.assertEqual(status, 200)

    # ── Origin check (POST only) ─────────────────────────────────────────

    def test_post_rejects_foreign_origin(self):
        status, body = self._post(server.ROUTE_PREFIX + "/api/color",
                                  {"path": "/tmp/x", "color": "#fff"},
                                  headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)
        self.assertIn("Forbidden", json.loads(body)["message"])

    def test_post_allows_missing_origin(self):
        # Same-origin browser requests to a plain HTTP endpoint often omit
        # Origin entirely — must not be treated as foreign.
        status, _ = self._post(server.ROUTE_PREFIX + "/api/does-not-exist", {})
        self.assertEqual(status, 404)  # past the Origin check, into routing

    def test_post_allows_localhost_origin(self):
        status, _ = self._post(server.ROUTE_PREFIX + "/api/does-not-exist", {},
                                headers={"Origin": "http://127.0.0.1:8477"})
        self.assertEqual(status, 404)  # past the Origin check, into routing


class _StubHandler:
    """Minimal stand-in for RequestHandler — just enough to call a _get_*/
    _post_* method directly without spinning up a real socket."""
    def __init__(self):
        self.responses = []

    def send_json(self, data, code=200):
        self.responses.append((code, data))


class TypeValidationRegressionTests(unittest.TestCase):
    """One of round 1's routing-refactor benefits: individual endpoint
    methods are now directly callable. These guard the round-2 fixes for
    _post_api_reorder/_post_api_config accepting wrong-typed client values
    that used to persist bad data and then crash scan_accounts() app-wide
    on every subsequent call (TypeError in a sort()/os.path call)."""

    def setUp(self):
        self._orig_order = copy.deepcopy(state.metadata.get("order", {}))
        self._orig_extra_dirs = state.config.get("extra_scan_dirs")
        self._orig_app_source = state.config.get("app_source")
        self._orig_keeper_secs = state.config.get("keeper_open_seconds")
        self._orig_keeper_days = state.config.get("keeper_interval_days")

    def tearDown(self):
        state.metadata["order"] = self._orig_order
        state.config["extra_scan_dirs"] = self._orig_extra_dirs
        state.config["app_source"] = self._orig_app_source
        state.config["keeper_open_seconds"] = self._orig_keeper_secs
        state.config["keeper_interval_days"] = self._orig_keeper_days

    def test_reorder_rejects_non_int_value(self):
        stub = _StubHandler()
        path = os.path.join(server.ROOT_DIR, "does-not-exist")
        with mock.patch("server.save_metadata"):
            server.RequestHandler._post_api_reorder(stub, {"orders": {path: "1"}})
        _, body = stub.responses[0]
        self.assertFalse(body["success"])
        self.assertNotIn(path, state.metadata.get("order", {}))

    def test_reorder_rejects_bool_value(self):
        # bool is an int subclass in Python — must be excluded explicitly,
        # or True/False would silently become order 1/0.
        stub = _StubHandler()
        path = os.path.join(server.ROOT_DIR, "does-not-exist")
        with mock.patch("server.save_metadata"):
            server.RequestHandler._post_api_reorder(stub, {"orders": {path: True}})
        _, body = stub.responses[0]
        self.assertFalse(body["success"])

    def test_reorder_accepts_int_value(self):
        stub = _StubHandler()
        path = os.path.join(server.ROOT_DIR, "does-not-exist")
        with mock.patch("server.save_metadata"):
            server.RequestHandler._post_api_reorder(stub, {"orders": {path: 3}})
        _, body = stub.responses[0]
        self.assertTrue(body["success"])
        self.assertEqual(state.metadata["order"][path], 3)

    def test_config_rejects_non_string_extra_scan_dir(self):
        stub = _StubHandler()
        with mock.patch("server.save_config"):
            server.RequestHandler._post_api_config(stub, {"extra_scan_dirs": [123]})
        _, body = stub.responses[0]
        self.assertIn("extra_scan_dirs", " ".join(body["rejected"]))
        self.assertNotEqual(state.config.get("extra_scan_dirs"), [123])

    def test_config_rejects_non_string_app_source(self):
        stub = _StubHandler()
        with mock.patch("server.save_config"):
            server.RequestHandler._post_api_config(stub, {"app_source": 123})
        _, body = stub.responses[0]
        self.assertIn("app_source", " ".join(body["rejected"]))
        self.assertNotEqual(state.config.get("app_source"), 123)

    def test_config_clamps_out_of_range_keeper_open_seconds(self):
        stub = _StubHandler()
        with mock.patch("server.save_config"):
            server.RequestHandler._post_api_config(stub, {"keeper_open_seconds": -5})
        self.assertEqual(state.config.get("keeper_open_seconds"), 30)

    def test_config_clamps_out_of_range_keeper_interval_days(self):
        stub = _StubHandler()
        with mock.patch("server.save_config"):
            server.RequestHandler._post_api_config(stub, {"keeper_interval_days": 9999})
        self.assertEqual(state.config.get("keeper_interval_days"), 365)


if __name__ == "__main__":
    unittest.main()
