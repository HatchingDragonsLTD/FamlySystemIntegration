"""The suite-wide guard: credentials blanked, real outbound requests refused, fake transports allowed."""

import json
import os
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import requests

from tests import network_guard as guard      # first: it blanks the credentials before core.config loads
from core.hubspot import HubSpotDealWriter  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID", "SLACK_SIGNING_SECRET", "HUBSPOT_TOKEN",
               "FAMLY_ACCESS_TOKEN", "INTAKE_SHARED_SECRET")


class CredentialTests(unittest.TestCase):
    def test_blank_credentials_blanks_the_credential_names_and_nothing_else(self):
        env = {"SLACK_BOT_TOKEN": "x", "HUBSPOT_TOKEN": "x", "FAMLY_ACCESS_TOKEN": "x", "FAMLY_GRAPHQL_URL": "u",
               "INTAKE_SHARED_SECRET": "x", "SOME_PASSWORD": "x", "MY_API_KEY": "x", "COMMIT_ENABLED": "true",
               "PREVIEW_STORE_PATH": "/tmp/s", "PATH": "/bin"}
        blanked = guard.blank_credentials(env)
        self.assertEqual(blanked, sorted(["SLACK_BOT_TOKEN", "HUBSPOT_TOKEN", "FAMLY_ACCESS_TOKEN", "FAMLY_GRAPHQL_URL",
                                          "INTAKE_SHARED_SECRET", "SOME_PASSWORD", "MY_API_KEY"]))
        self.assertTrue(all(env[name] == "" for name in blanked))
        self.assertEqual((env["COMMIT_ENABLED"], env["PREVIEW_STORE_PATH"], env["PATH"]), ("true", "/tmp/s", "/bin"))

    def test_a_process_that_imports_the_tests_package_never_sees_the_real_credentials(self):
        """Even with live-looking values in its environment, and with core.config loading the real .env."""
        env = {**os.environ, **{name: "live-looking-value" for name in CREDENTIALS}}
        code = ("import json, os, tests\nimport core.config\n"
                f"print(json.dumps({{name: os.environ.get(name) for name in {list(CREDENTIALS)!r}}}))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {name: "" for name in CREDENTIALS})


class BlockedTests(unittest.TestCase):
    def test_a_real_request_raises_a_clear_error_and_is_recorded(self):
        with guard.expected_attempts():
            with self.assertRaises(guard.RealNetworkAttempted) as ctx:
                requests.post("https://api.hubapi.com/crm/v3/objects/deals/1", json={"properties": {}})
            self.assertIn("fake transport", str(ctx.exception))
            self.assertEqual(guard.ATTEMPTS[-1], "POST api.hubapi.com")           # method and host only
            for call in (requests.get, requests.patch, requests.put, requests.delete):
                with self.assertRaises(guard.RealNetworkAttempted):
                    call("https://slack.com/api/chat.postMessage")

    def test_the_real_hubspot_writer_cannot_reach_hubspot_even_with_a_token(self):
        """The incident: a test whose path ended in the real HubSpotDealWriter."""
        with guard.expected_attempts(), mock.patch.dict(os.environ, {"HUBSPOT_TOKEN": "pat-fake"}):
            with self.assertRaises(guard.RealNetworkAttempted):
                HubSpotDealWriter().write_plan_uploaded("5550001", "true")

    def test_code_that_catches_every_exception_cannot_swallow_it(self):
        with guard.expected_attempts():
            with self.assertRaises(guard.RealNetworkAttempted):
                try:
                    requests.get("https://example.com/")
                except Exception:                                                # noqa: BLE001
                    self.fail("swallowed")

    def test_other_clients_are_stopped_at_the_socket(self):
        with guard.expected_attempts():
            with self.assertRaises(guard.RealNetworkAttempted):
                urllib.request.urlopen("http://example.com/", timeout=1)
            with self.assertRaises(guard.RealNetworkAttempted):
                import socket
                socket.create_connection(("example.org", 80), timeout=1)

    def test_a_loopback_connection_is_not_the_guards_business(self):
        import socket
        before = len(guard.ATTEMPTS)
        with self.assertRaises(OSError):                                         # nothing listens on port 1
            socket.create_connection(("127.0.0.1", 1), timeout=1)
        self.assertEqual(len(guard.ATTEMPTS), before)


class OptInTests(unittest.TestCase):
    def test_a_fake_transport_answers_the_call_and_the_guard_returns_afterwards(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url))
            return guard.response(201, {"ok": True})
        with guard.fake_transport(handler):
            reply = requests.post("https://api.hubapi.com/crm/v3/objects/deals/1", json={"a": 1})
            self.assertEqual((reply.status_code, reply.json()), (201, {"ok": True}))
        self.assertEqual(seen, [("POST", "https://api.hubapi.com/crm/v3/objects/deals/1")])
        with guard.expected_attempts(), self.assertRaises(guard.RealNetworkAttempted):
            requests.get("https://api.hubapi.com/")

    def test_a_session_with_its_own_mounted_adapter_needs_no_opt_in(self):
        class FakeAdapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):
                return guard.response(200, {"fake": True})

            def close(self):
                pass
        session = requests.Session()
        session.mount("https://", FakeAdapter())
        self.assertEqual(session.get("https://api.hubapi.com/x").json(), {"fake": True})


if __name__ == "__main__":
    unittest.main()
