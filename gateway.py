"""Authenticated HTTP gateway for operator-configured model and desktop services."""

import argparse
import collections
import hmac
import http.server
import ipaddress
import json
import os
import pathlib
import re
import socket
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from wb_stream_adapter import prepare_request


GROUP_PATH = re.compile(r"^/(v(?:[1-9]|10))/(?:v1/)?(models|chat/completions|responses|messages)$")
NATIVE_PATH = re.compile(
    r"^/workbaby/v1/(capabilities|sessions|sessions/[A-Za-z0-9_-]{1,128}/turns|"
    r"turns/[A-Za-z0-9_-]{1,128}(?:/(?:events|cancel|approvals/[A-Za-z0-9_-]{1,128}))?)$"
)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,79}$")
MAX_BODY = 1024 * 1024
COPY_HEADERS = ("Last-Event-ID", "Anthropic-Version", "Anthropic-Beta")


class RequestError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secret(name, env, minimum=1):
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        raise ValueError("credential references must be environment variable names")
    value = env.get(name)
    secret_file = env.get(name + "_FILE")
    if value and secret_file:
        raise ValueError(f"set either {name} or {name}_FILE")
    if secret_file:
        value = pathlib.Path(secret_file).read_text(encoding="utf-8").strip()
    if not value or len(value) < minimum or not value.isascii() or any(not 33 <= ord(x) <= 126 for x in value):
        raise ValueError(f"configure {name} with a local credential")
    return value


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def upstream_url(url, kind):
    parsed = urllib.parse.urlsplit(url)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query
            or parsed.fragment or "\\" in url or not url.isascii() or any(ord(ch) < 33 for ch in url)):
        raise ValueError("invalid upstream URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback(parsed.hostname)):
        raise ValueError("upstreams must use HTTPS or loopback HTTP")
    if kind == "native" and parsed.path.rstrip("/") != "/workbaby/v1":
        raise ValueError("native upstream base URL must end in /workbaby/v1")
    return url.rstrip("/")


@dataclass(frozen=True)
class Upstream:
    base: str
    token: str
    auth: str
    normalize_stream: bool = False

    def headers(self):
        if self.auth == "x-api-key":
            return {"x-api-key": self.token}
        return {"Authorization": "Bearer " + self.token}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    token: str
    native: Upstream | None
    groups: dict


def read_upstream(row, kind, env):
    auth = row.get("auth", "bearer")
    if auth not in ("bearer", "x-api-key") or (kind == "native" and auth != "bearer"):
        raise ValueError("invalid upstream authentication type")
    normalize = row.get("normalize_stream", False)
    if not isinstance(normalize, bool):
        raise ValueError("normalize_stream must be a boolean")
    return Upstream(upstream_url(row["base_url"], kind), secret(row["token_env"], env), auth, normalize)


def load_settings(file, env):
    with open(file, "rb") as handle:
        config = tomllib.load(handle)
    host = config.get("listen", {}).get("host", "127.0.0.1")
    port = config.get("listen", {}).get("port", 8788)
    if host not in ("127.0.0.1", "0.0.0.0") or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid listen.host or listen.port")
    token = secret("WB_GATEWAY_TOKEN", env, minimum=32)
    native = read_upstream(config["native"], "native", env) if "native" in config else None
    groups = {}
    for group, row in config.get("groups", {}).items():
        if not re.fullmatch(r"v(?:[1-9]|10)", group):
            raise ValueError("invalid API group")
        models = row.get("models", [])
        routes = row.get("routes", ["chat/completions"])
        if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError("each API group requires explicit model IDs")
        if not isinstance(routes, list) or not routes or any(r not in ("chat/completions", "responses", "messages") for r in routes):
            raise ValueError("invalid group routes")
        groups[group] = (read_upstream(row, "model", env), tuple(dict.fromkeys(models)), frozenset(routes))
    credentials = [upstream.token for upstream, _, _ in groups.values()]
    if native:
        credentials.append(native.token)
    if any(hmac.compare_digest(token, value) for value in credentials):
        raise ValueError("gateway and upstream credentials must differ")
    return Settings(host, port, token, native, groups)


class Gateway(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, settings):
        self.settings = settings
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.lock = threading.Lock()
        self.counts = collections.Counter()
        self.slots = threading.BoundedSemaphore(16)
        super().__init__((settings.host, settings.port), Handler)

    def counter(self, key):
        with self.lock:
            self.counts[key] += 1

    def status(self):
        with self.lock:
            counts = dict(self.counts)
        return {
            "native_configured": self.settings.native is not None,
            "groups": {group: {"models": models, "routes": sorted(routes)}
                       for group, (_, models, routes) in self.settings.groups.items()},
            "requests": counts,
        }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "workbuddy2api"

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def body(self, allow_empty=False):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "unsupported_transfer_encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if allow_empty and lengths in ([], ["0"]):
            return None
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            raise RequestError(411, "content_length_required")
        length = int(lengths[0])
        if length < 1 or length > MAX_BODY:
            raise RequestError(413, "request_body_outside_limit")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise RequestError(400, "incomplete_request_body")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise RequestError(400, "invalid_json") from error
        if not isinstance(body, dict):
            raise RequestError(400, "expected_json_object")
        return body

    def local(self, status, content, kind="application/json; charset=utf-8"):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def dispatch(self):
        acquired = False
        try:
            try:
                route = urllib.parse.urlsplit(self.path)
            except ValueError as error:
                raise RequestError(400, "invalid_request_target") from error
            if route.scheme or route.netloc or route.fragment:
                raise RequestError(400, "invalid_request_target")
            if self.command == "GET" and route.path in ("/", "/dashboard.js", "/dashboard.css"):
                filename, kind = {
                    "/": ("dashboard.html", "text/html; charset=utf-8"),
                    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                }[route.path]
                return self.local(200, pathlib.Path(__file__).with_name(filename).read_bytes(), kind)
            if self.command == "GET" and route.path == "/health":
                return self.local(200, {"status": "ok"})
            auth = self.headers.get_all("Authorization", [])
            expected = ("Bearer " + self.server.settings.token).encode("ascii")
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode("iso-8859-1"), expected):
                raise RequestError(401, "unauthorized")
            if self.command == "GET" and route.path == "/api/status":
                return self.local(200, self.server.status())
            acquired = self.server.slots.acquire(blocking=False)
            if not acquired:
                raise RequestError(503, "gateway_at_capacity")
            native = NATIVE_PATH.fullmatch(route.path)
            if native:
                return self.native(route, native[1])
            group = GROUP_PATH.fullmatch(route.path)
            if group:
                return self.model(route, group[1], group[2])
            raise RequestError(404, "route_not_found")
        except RequestError as error:
            self.server.counter("rejected")
            self.local(error.status, {"error": {"code": error.code}})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            if acquired:
                self.server.slots.release()

    def native(self, route, suffix):
        upstream = self.server.settings.native
        if not upstream:
            raise RequestError(404, "native_upstream_not_configured")
        readable = suffix == "capabilities" or (suffix.startswith("turns/") and
                   (suffix.count("/") == 1 or suffix.endswith("/events")))
        if (self.command == "GET") != readable:
            raise RequestError(405, "method_not_allowed")
        query = ""
        if route.query:
            if not suffix.endswith("/events") or not re.fullmatch(r"after=[0-9]{1,16}", route.query):
                raise RequestError(400, "unsupported_query")
            query = "?" + route.query
        return self.relay(upstream, suffix + query, None if readable else self.body(allow_empty=suffix.endswith("/cancel")))

    def model(self, route, group, suffix):
        configured = self.server.settings.groups.get(group)
        if not configured:
            raise RequestError(404, "api_group_not_configured")
        upstream, models, routes = configured
        if route.query:
            raise RequestError(400, "unsupported_query")
        if suffix == "models":
            if self.command != "GET":
                raise RequestError(405, "method_not_allowed")
            return self.local(200, {"object": "list", "data": [{"id": model, "object": "model"} for model in models]})
        if self.command != "POST":
            raise RequestError(405, "method_not_allowed")
        if suffix not in routes:
            raise RequestError(404, "api_route_not_configured")
        body = self.body()
        if body.get("model") not in models:
            raise RequestError(400, "model_not_configured")
        return self.relay(upstream, suffix, body)

    def relay(self, upstream, suffix, body):
        headers = {"Accept": "application/json, text/event-stream", **upstream.headers()}
        for name in COPY_HEADERS:
            value = self.headers.get(name)
            if value and len(value) <= 256 and "\r" not in value and "\n" not in value:
                headers[name] = value
        if body is not None:
            headers["Content-Type"] = "application/json"
            if suffix == "chat/completions" and body.get("stream") is True and upstream.normalize_stream:
                body, headers = prepare_request(body, headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(upstream.base + "/" + suffix, data=data, headers=headers, method=self.command)
        self.server.counter("dispatched")
        try:
            response = self.server.opener.open(req, timeout=45)
        except urllib.error.HTTPError as error:
            error.close()
            self.server.counter("upstream_error")
            status = error.code if 400 <= error.code <= 599 else 502
            return self.local(status, {"error": {"code": "upstream_rejected", "upstream_status": error.code}})
        except (urllib.error.URLError, OSError, ValueError):
            self.server.counter("upstream_error")
            return self.local(502, {"error": {"code": "upstream_unavailable"}})
        with response:
            mime = response.headers.get_content_type()
            content_encoding = response.headers.get("Content-Encoding", "identity").lower()
            if mime not in ("application/json", "text/event-stream") or content_encoding != "identity":
                self.server.counter("upstream_error")
                return self.local(502, {"error": {"code": "unsupported_upstream_response"}})
            self.send_response(response.status)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            read = response.readline if mime == "text/event-stream" else response.read
            try:
                while chunk := read(65536):
                    self.wfile.write(chunk)
                    if mime == "text/event-stream":
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
                self.server.counter("interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to local TOML configuration")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config, os.environ)
        with Gateway(settings) as server:
            print(f"workbuddy2api listening on {server.server_address[0]}:{server.server_address[1]}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"configuration or startup failed: {error}") from None


if __name__ == "__main__":
    main()
