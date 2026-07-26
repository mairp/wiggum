#!/usr/bin/env python3
"""_test_http.py — a tiny in-process HTTP capture server for telemetry tests.

The wiggum test suite is stdlib-only and had no HTTP-stubbing precedent (the only
prior test, test_critic.py, is pure filesystem). Both the Loki shipper and the OTEL
shipper POST JSON with urllib, so their tests need to observe what actually goes on
the wire without a real Loki / OTLP collector.

`CaptureServer` binds an ephemeral port on 127.0.0.1, records every POST (path +
parsed JSON body), and can be told to answer with a chosen status so the tests can
also exercise the shippers' best-effort "never raise" contract.

Usage:
    with CaptureServer() as srv:
        run_something(srv.url)          # e.g. "http://127.0.0.1:54321"
        assert srv.requests[0].path == "/loki/api/v1/push"
        assert srv.json_at("/v1/logs")["resourceLogs"]

No third-party deps; safe to import from python3 lib/test_*.py or under pytest.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Captured:
    """One recorded request."""
    __slots__ = ("path", "headers", "raw", "json")

    def __init__(self, path, headers, raw):
        self.path = path
        self.headers = headers
        self.raw = raw
        try:
            self.json = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            self.json = None


class CaptureServer:
    """Threaded HTTP server that records POSTs. Use as a context manager.

    status: the HTTP status every request is answered with (default 204, matching
            a happy Loki push). Set to 500 to exercise the error path.
    """

    def __init__(self, status=204):
        self.requests = []          # list[Captured] in arrival order
        self._status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):     # silence stderr access logs
                pass

            def do_POST(self):             # noqa: N802 (stdlib naming)
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                outer.requests.append(Captured(self.path, dict(self.headers), raw))
                self.send_response(outer._status)
                self.end_headers()

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- context manager -----------------------------------------------------
    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass

    # -- convenience accessors ----------------------------------------------
    def paths(self):
        return [r.path for r in self.requests]

    def at(self, path):
        """All captured requests whose path endswith the given suffix."""
        return [r for r in self.requests if r.path.endswith(path)]

    def json_at(self, path):
        """Parsed JSON body of the FIRST request whose path endswith `path`."""
        for r in self.requests:
            if r.path.endswith(path):
                return r.json
        raise AssertionError("no request to %r; saw %r" % (path, self.paths()))
