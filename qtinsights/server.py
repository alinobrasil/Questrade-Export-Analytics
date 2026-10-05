"""Local web server: serves the dashboard and its data on 127.0.0.1 only."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import analytics, config
from .questrade import AuthError
from .store import Store
from .sync import sync

STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self):
        self.store = Store(config.DB_FILE)
        self.lock = threading.Lock()
        self.sync_log = []
        self.syncing = False
        self.sync_error = None

    def run_sync(self, token=None):
        def log(msg, end="\n"):
            if end == "\r" and self.sync_log:  # progress line: replace in place
                self.sync_log[-1] = msg
            else:
                self.sync_log.append(msg)
        try:
            with self.lock:
                sync(self.store, token, log)
        except AuthError as e:
            self.sync_error = str(e)
        except Exception as e:  # surface anything else in the UI rather than dying silently
            self.sync_error = f"Sync failed: {e}"
        finally:
            self.syncing = False


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self.send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/summary":
                with state.lock:
                    has_data = bool(state.store.accounts())
                    data = analytics.build(state.store) if has_data else None
                    needs_scope = state.store.meta("price_error") == "scope"
                return self.send(200, {"data": data, "has_token": config.TOKEN_FILE.exists(),
                                       "needs_market_data": needs_scope})
            if self.path == "/api/sync":
                return self.send(200, {"syncing": state.syncing, "log": state.sync_log[-12:],
                                       "error": state.sync_error})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/sync":
                return self.send(404, {"error": "not found"})
            # Only accept requests from this app's own page, not other sites in the browser.
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host')}":
                return self.send(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if state.syncing:
                return self.send(409, {"error": "A sync is already running."})
            state.syncing, state.sync_log, state.sync_error = True, [], None
            threading.Thread(target=state.run_sync, args=(body.get("token") or None,), daemon=True).start()
            self.send(202, {"started": True})

    return Handler


def serve(port=8765):
    state = State()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    print(f"Questrade Insights running at http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
