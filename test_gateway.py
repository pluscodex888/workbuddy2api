import contextlib
import http.client
import http.server
import json
import pathlib
import secrets
import tempfile
import threading
import unittest

from gateway import Gateway, load_settings


@contextlib.contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class Fixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        size = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(size)) if size else None
        self.server.requests.append((self.command, self.path, dict(self.headers), data))
        if data and data.get("case") == "redirect":
            self.send_response(302)
            self.send_header("Location", "/credential-sink")
            self.end_headers()
            return
        if data and data.get("case") == "upstream_error":
            self.send_response(429)
            self.end_headers()
            self.wfile.write(self.headers.get("Authorization", "").encode("ascii"))
            return
        self.send_response(200)
        if self.path.endswith("/events?after=2") or (data and data.get("stream")):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: {\"ok\":true}\n\ndata: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.upstream = self.stack.enter_context(serving(http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)))
        self.upstream.requests = []
        root = pathlib.Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.local_key = secrets.token_urlsafe(32)
        self.remote_key = secrets.token_urlsafe(32)
        self.config = root / "gateway.local.toml"
        host = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.config.write_text(f"""
[listen]
port = 0
[native]
base_url = "{host}/workbaby/v1"
token_env = "WB_NATIVE_TOKEN"
[groups.v1]
base_url = "{host}/v1"
token_env = "WB_UPSTREAM_TOKEN"
models = ["sample-model"]
routes = ["chat/completions", "responses"]
normalize_stream = true
[groups.v2]
base_url = "{host}/v1"
token_env = "WB_UPSTREAM_TOKEN"
auth = "x-api-key"
models = ["sample-messages"]
routes = ["messages"]
""", encoding="utf-8")
        self.env = {
            "WB_GATEWAY_TOKEN": self.local_key, "WB_NATIVE_TOKEN": self.remote_key,
            "WB_UPSTREAM_TOKEN": self.remote_key,
        }
        self.gateway = self.stack.enter_context(serving(Gateway(load_settings(self.config, self.env))))

    def request(self, method, path, payload=None, authenticated=True, **headers):
        if authenticated:
            headers["Authorization"] = "Bearer " + self.local_key
        encoded = json.dumps(payload).encode() if payload is not None else None
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway.server_address[1], timeout=3)
        try:
            conn.request(method, path, encoded, headers)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            conn.close()

    def test_dashboard_only_returns_aggregate_status_with_authentication(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/status", authenticated=False)[0], 401)
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn(self.local_key.encode(), body)
        self.assertNotIn(self.remote_key.encode(), body)
        self.assertIn("sample-model", json.loads(body)["groups"]["v1"]["models"])
        self.assertEqual(self.upstream.requests, [])

    def test_model_group_denial_is_local_and_success_streams_once(self):
        self.assertEqual(self.request("POST", "/v1/chat/completions", {"model": "other"})[0], 400)
        status, media, body = self.request("POST", "/v1/chat/completions", {"model": "sample-model", "stream": True})
        self.assertEqual(status, 200)
        self.assertTrue(media.startswith("text/event-stream"))
        self.assertIn(b"data: [DONE]", body)
        self.assertEqual(len(self.upstream.requests), 1)
        _, path, headers, data = self.upstream.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer " + self.remote_key)
        self.assertEqual(data["stream_options"], {"include_usage": True})

    def test_messages_uses_its_own_credential_header(self):
        status, _, _ = self.request("POST", "/v2/v1/messages", {"model": "sample-messages"}, **{"Anthropic-Version": "test"})
        self.assertEqual(status, 200)
        _, route, headers, _ = self.upstream.requests[0]
        self.assertEqual(route, "/v1/messages")
        self.assertEqual(headers["X-Api-Key"], self.remote_key)
        self.assertNotIn("Authorization", headers)

    def test_native_stream_cursor_and_bodyless_cancel(self):
        event_route = "/workbaby/v1/turns/test-id/events?after=2"
        self.assertEqual(self.request("GET", event_route)[0], 200)
        cancel_route = "/workbaby/v1/turns/test-id/cancel"
        self.assertEqual(self.request("POST", cancel_route)[0], 200)
        self.assertEqual([row[1] for row in self.upstream.requests], [event_route, cancel_route])
        self.assertEqual(self.request("GET", "/workbaby/v1/private/raw")[0], 404)

    def test_blocked_redirect_does_not_forward_the_upstream_credential(self):
        status, _, _ = self.request("POST", "/v1/chat/completions", {"model": "sample-model", "case": "redirect"})
        self.assertEqual(status, 502)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_upstream_errors_do_not_echo_credential_or_retry(self):
        status, _, body = self.request("POST", "/v1/chat/completions", {"model": "sample-model", "case": "upstream_error"})
        self.assertEqual(status, 429)
        self.assertNotIn(self.remote_key.encode(), body)
        self.assertEqual(len(self.upstream.requests), 1)

    def test_startup_requires_a_distinct_gateway_key(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            load_settings(self.config, dict(self.env, WB_GATEWAY_TOKEN=self.remote_key))
