"""Host and Origin checks against DNS rebinding and cross-site requests (T03).

Loopback clients skip the access token, so the hub must reject requests whose
Host header names a foreign site (DNS rebinding) and POSTs that a page from
another origin sends. Requests with a valid token and same-origin requests keep
working, so LAN mode and the module UIs are unchanged.
"""
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Module, ModuleRegistry  # noqa: E402
from core.server import HubHandler, Settings, ThreadedHTTPServer  # noqa: E402

TOKEN = "T03-test-token-0123456789abcdef"


class Probe(Module):
    name = "Probe"

    def routes_get(self):
        return {
            "/api/probe": lambda handler, qs: handler.respond_json({"ok": True}),
            "/probe": lambda handler, qs: handler.respond_html("<p>probe page</p>"),
        }

    def routes_post(self):
        return {"/api/probe": self.post}

    def post(self, handler, content_len, content_type):
        data = handler.read_body_json(content_len) if content_len else None
        handler.respond_json({"ok": True, "data": data})


class RequestOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = Settings(os.path.join(cls.tmp.name, "settings.json"))
        registry = ModuleRegistry()
        registry.register(Probe(None))
        cls.saved = {name: getattr(HubHandler, name) for name in ("registry", "settings", "hub", "require_auth", "auth_token")}
        HubHandler.registry = registry
        HubHandler.settings = cls.settings
        HubHandler.hub = None
        HubHandler.require_auth = False
        HubHandler.auth_token = ""
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), HubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for name, value in cls.saved.items():
            setattr(HubHandler, name, value)
        cls.tmp.cleanup()

    def setUp(self):
        HubHandler.require_auth = False
        HubHandler.auth_token = ""
        self.settings.set_path("network.allowed_hosts", [])

    def request(self, method="GET", path="/api/probe", host=None, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
        payload = body.encode() if isinstance(body, str) else body
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        if payload is not None:
            connection.putheader("Content-Length", str(len(payload)))
        connection.endheaders(payload)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def post_json(self, **kwargs):
        headers = {"Content-Type": "application/json", **kwargs.pop("headers", {})}
        return self.request("POST", headers=headers, body=json.dumps({"x": 1}), **kwargs)

    # Host header (DNS rebinding)

    def test_loopback_names_and_ip_addresses_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"LOCALHOST:{self.port}",
                     f"[::1]:{self.port}", f"desktop.localhost:{self.port}", f"192.168.1.20:{self.port}", "127.0.0.1"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 200)

    def test_foreign_host_name_is_rejected_for_loopback_clients(self):
        status, body = self.request(host=f"rebind.attacker.example:{self.port}")
        self.assertEqual(status, 421)
        self.assertIn("network.allowed_hosts", json.loads(body)["error"])
        status, body = self.request(path="/probe", host="rebind.attacker.example")
        self.assertEqual(status, 421)
        self.assertIn(b"Request blocked", body)

    def test_malformed_host_is_rejected(self):
        self.assertEqual(self.request(host="localhost:notaport")[0], 421)

    def test_this_computers_own_name_is_accepted(self):
        name = socket.gethostname().lower()
        self.assertEqual(self.request(host=f"{name}:{self.port}")[0], 200)
        self.assertEqual(self.request(host=f"{name.split('.')[0]}.local:{self.port}")[0], 200)

    def test_configured_host_names_are_accepted(self):
        self.settings.set_path("network.allowed_hosts", ["Hub.Example.lan"])
        self.assertEqual(self.request(host=f"hub.example.lan:{self.port}")[0], 200)
        with patch.dict(os.environ, {"CYBERHUB_ALLOWED_HOSTS": "nas.example, other.example"}):
            self.assertEqual(self.request(host="nas.example")[0], 200)
        self.assertEqual(self.request(host="nas.example")[0], 421)

    def test_valid_token_skips_the_host_check(self):
        HubHandler.require_auth = True
        HubHandler.auth_token = TOKEN
        self.assertEqual(self.request(host="gaming-pc.example.lan", headers={"Authorization": f"Bearer {TOKEN}"})[0], 200)
        self.assertEqual(self.request(host="gaming-pc.example.lan", headers={"Cookie": f"cdhub_token={TOKEN}"})[0], 200)
        self.assertEqual(self.request(host="gaming-pc.example.lan", headers={"Authorization": "Bearer wrong-token"})[0], 421)

    # Origin and Sec-Fetch-Site on POST (CSRF)

    def test_same_origin_post_is_accepted(self):
        status, body = self.post_json(headers={"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"], {"x": 1})
        self.assertEqual(self.post_json(host=f"localhost:{self.port}", headers={"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_post_without_origin_is_accepted(self):
        # Non-browser clients (curl, scripts, the desktop app's own requests).
        self.assertEqual(self.post_json()[0], 200)
        self.assertEqual(self.request("POST")[0], 200)  # body-less action POST

    def test_cross_origin_post_is_rejected(self):
        for origin in ("http://attacker.example", f"http://localhost:{self.port + 1}", "null",
                       f"https://127.0.0.1:{self.port}0", "file://"):
            with self.subTest(origin=origin):
                self.assertEqual(self.post_json(headers={"Origin": origin})[0], 403)

    def test_cross_site_fetch_metadata_is_rejected(self):
        self.assertEqual(self.post_json(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request(headers={"Sec-Fetch-Site": "cross-site"})[0], 200)  # links into the hub still work

    def test_cross_origin_post_with_token_is_still_rejected(self):
        HubHandler.require_auth = True
        HubHandler.auth_token = TOKEN
        status, _ = self.post_json(headers={"Origin": "http://attacker.example", "Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 403)

    def test_configured_proxy_origin_is_accepted(self):
        self.settings.set_path("network.allowed_hosts", ["hub.example.lan"])
        self.assertEqual(self.post_json(headers={"Origin": "https://hub.example.lan"})[0], 200)
        self.assertEqual(self.post_json(headers={"Origin": "https://other.example.lan"})[0], 403)

    # Body types a cross-site page can send without a CORS preflight

    def test_simple_cors_body_types_are_rejected(self):
        for content_type in ("text/plain;charset=UTF-8", "application/x-www-form-urlencoded"):
            with self.subTest(content_type=content_type):
                status, _ = self.request("POST", headers={"Content-Type": content_type}, body='{"x": 1}')
                self.assertEqual(status, 415)

    def test_json_and_multipart_bodies_are_unchanged(self):
        self.assertEqual(self.post_json()[0], 200)
        body = "--b\r\nContent-Disposition: form-data; name=\"a\"\r\n\r\n1\r\n--b--\r\n"
        self.assertEqual(self.request("POST", headers={"Content-Type": "multipart/form-data; boundary=b"}, body=body)[0], 200)

    def test_rejected_post_keeps_the_server_responsive(self):
        self.assertEqual(self.post_json(headers={"Origin": "http://attacker.example"})[0], 403)
        self.assertEqual(self.request()[0], 200)


if __name__ == "__main__":
    unittest.main()
