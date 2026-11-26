"""The server: Python's http.server and sqlite3, nothing else.

    POST /v1/events   {"events": [...]}  the sender's batches (a key, when the server has one)
    GET  /v1/query    measure, by, filters, since, until, bucket  (see query.py)
    GET  /v1/meta     kinds, time range, measures, labels and their values
    GET  /v1/events   the rows behind a slice, newest first (the same filters as a query)
    GET  /v1/seat     one seat close up: summary, setup, helpers, latest behavior graphs (see detail.py)
    GET  /v1/graphs   behavior graphs agents wrote, with what they scored
    GET  /v1/helpers  which helpers each seat has on, and how often each is used (see helpers.py)
    GET  /v1/flow     what agents call next: paths of tool calls through turns (see flow.py)
    GET  /v1/seats    one row per seat
    GET  /v1/health   ok and the row count
    GET  /            the dashboard

It listens on 127.0.0.1 unless told otherwise, and will not listen any wider without a key. With a key every /v1 call
needs `Authorization: Bearer <key>` (the dashboard asks for it once and keeps it in the browser), unless
--open-read lets anyone read."""

from __future__ import annotations

import hmac
import ipaddress
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .detail import graphs, seat_detail
from .flow import flow
from .helpers import helpers
from .query import QueryError, events, meta, run_query, seats
from .store import Store

MAX_BODY = 16 * 1024 * 1024
MAX_EVENTS = 5000
PAGE = Path(__file__).with_name("dashboard.html")


def loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class App:
    def __init__(self, store: Store, key: str = "", max_detail: str = "full", open_read: bool = False):
        self.store, self.key, self.max_detail, self.open_read = store, key, max_detail, open_read
        self.taken = 0
        self._meta: tuple[int, dict[str, Any]] | None = None
        self._busy = threading.Lock()

    def meta(self) -> dict[str, Any]:
        """What the store holds. Worked out once, then again in the background when rows have come in: until that
        is done the last answer is served, so a page never waits on it."""
        last = self.store.execute("SELECT MAX(id) FROM events")[0][0] or 0
        if self._meta is None:
            self._meta = (last, meta(self.store))
        elif self._meta[0] != last and not self._busy.locked():
            threading.Thread(target=self._refresh, args=(last,), daemon=True).start()
        return self._meta[1]

    def _refresh(self, last: int) -> None:
        with self._busy:
            self._meta = (last, meta(self.store))


def handler(app: App) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        server_version = f"body-telemetry/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {fmt % args}\n")

        def send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def authed(self) -> bool:
            if not app.key:
                return True
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {app.key}".encode())

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                try:
                    self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self.send(500, {"error": "the dashboard page is missing"})
                return
            if u.path == "/v1/health":
                self.send(200, {"ok": True, "version": __version__, "key": bool(app.key)})
                return
            if not u.path.startswith("/v1/"):
                self.send(404, {"error": "not found"})
                return
            if not (app.open_read or self.authed()):
                self.send(401, {"error": "a key is needed"})
                return
            params: dict[str, Any] = {k: ",".join(v) for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/v1/query":
                    self.send(200, run_query(app.store, params))
                elif u.path == "/v1/meta":
                    self.send(200, app.meta() | {"taken": app.taken, "detail": app.max_detail})
                elif u.path == "/v1/seat":
                    self.send(200, seat_detail(app.store, params))
                elif u.path == "/v1/graphs":
                    self.send(200, graphs(app.store, params))
                elif u.path == "/v1/helpers":
                    self.send(200, helpers(app.store, params))
                elif u.path == "/v1/flow":
                    self.send(200, flow(app.store, params))
                elif u.path == "/v1/events":
                    self.send(200, events(app.store, params))
                elif u.path == "/v1/seats":
                    self.send(200, {"rows": seats(app.store, params)})
                else:
                    self.send(404, {"error": "not found"})
            except QueryError as e:
                self.send(400, {"error": str(e)})

        def do_HEAD(self) -> None:
            self.send_response(200 if urlparse(self.path).path in ("/", "/index.html", "/v1/health") else 404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            u = urlparse(self.path)
            if u.path != "/v1/events":
                self.send(404, {"error": "not found"})
                return
            if not self.authed():
                self.send(401, {"error": "a key is needed"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.send(413, {"error": f"a batch is at most {MAX_BODY} bytes"})
                return
            try:
                body = json.loads(self.rfile.read(n) or b"null")
            except ValueError:
                self.send(400, {"error": "not JSON"})
                return
            events = body.get("events") if isinstance(body, dict) else body
            if not isinstance(events, list):
                self.send(400, {"error": "expected {\"events\": [...]}"})
                return
            if len(events) > MAX_EVENTS:
                self.send(413, {"error": f"at most {MAX_EVENTS} events a batch"})
                return
            # an event that is not one is skipped, not refused: a refused batch would be posted again forever
            new, skipped = app.store.insert(events, source="live", detail=app.max_detail)
            app.taken += len(events) - skipped
            self.send(200, {"ok": True, "taken": len(events) - skipped, "new": new, "skipped": skipped})

    return H


def serve(db: str, host: str, port: int, key: str = "", max_detail: str = "full", open_read: bool = False) -> int:
    if not loopback(host) and not key:
        print(f"refusing to listen on {host} without a key: set BODY_TELEMETRY_KEY (or the variable --key-env "
              f"names), or listen on 127.0.0.1", file=sys.stderr)
        return 2
    store = Store(db)
    app = App(store, key, max_detail, open_read)
    threading.Thread(target=app.meta, daemon=True).start()  # warm: the first page should not wait on it
    srv = ThreadingHTTPServer((host, port), handler(app))
    print(f"body-telemetry {__version__} on http://{host}:{port}/  db={db} rows={store.count()} "
          f"key={'yes' if key else 'no'} reads={'open' if open_read or not key else 'need the key'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        store.close()
    return 0
