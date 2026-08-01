#!/usr/bin/env python3
"""Localhost canary launcher for funnelmanager.

Serves a tiny redirect on 127.0.0.1 so you can bookmark ONE clean localhost URL
and land on the telemetry canary without the secret ever living in your bookmark.

Flow when you open the bookmark:
  http://localhost:8799/           (your bookmark; no secret)
    -> 302 https://x9bc433.win/debug/canary/on?t=<secret>   (secret injected here, from creds.env)
       -> the debug-session-gate EnvoyFilter validates t, sets the HttpOnly
          fm_debug session cookie, 302s to /debug/route/canary which sets the
          fm_route=canary selector, then 302s to /  (you're now on the canary;
          sign in as e2e-canary). fm_debug alone would route to STABLE; the
          fm_route=canary selector is what steers you to the canary pods.

  http://localhost:8799/off  -> https://x9bc433.win/debug/off  (clears both cookies)

The secret (FM_DEBUG_TOKEN) is read from ~/.config/fm-e2e/creds.env and never
printed. The only place it appears in the browser is the transient
/debug/canary/on?t= redirect (browser history), never your bookmark. The fm_debug
cookie itself is set server-side as HttpOnly, so page JS can't read it.

Run:  python3 launcher.py            (foreground; Ctrl-C to stop)
      FM_CANARY_LAUNCHER_PORT=9000 python3 launcher.py   (override port)
"""
from __future__ import annotations

import http.server
import os
import sys

CREDS = os.path.expanduser("~/.config/fm-e2e/creds.env")
HUB = os.environ.get("FM_HUB_ORIGIN", "https://x9bc433.win")
PORT = int(os.environ.get("FM_CANARY_LAUNCHER_PORT", "8799"))


def load_secret() -> str | None:
    try:
        with open(CREDS) as f:
            for line in f:
                if line.startswith("FM_DEBUG_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


SECRET = load_secret()


class Handler(http.server.BaseHTTPRequestHandler):
    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/on"):
            self._redirect(f"{HUB}/debug/canary/on?t={SECRET}")
        elif path == "/off":
            self._redirect(f"{HUB}/debug/off")
        else:
            self.send_error(404)

    def log_message(self, *_args) -> None:  # silence request logging (paths carry the secret)
        pass


def main() -> int:
    if not SECRET:
        print(f"error: no FM_DEBUG_TOKEN in {CREDS}", file=sys.stderr)
        return 1
    print(f"canary launcher: http://localhost:{PORT}  ->  {HUB}/debug/canary/on")
    print("  bookmark http://localhost:%d  (·/off to disable)  ·  Ctrl-C to stop" % PORT)
    try:
        http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
