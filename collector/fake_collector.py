"""A collector that fails on purpose, for each SDK's broken-collector tests.

    python fake_collector.py --mode MODE [--delay-ms N]

Prints the port it listens on as the first line of stdout. ``POST
/v0/events`` with a bearer key answers by mode; any other POST gets 404 or
401. ``GET /_received`` returns the sequence numbers it accepted, how many
requests it got, and how many ``client_report`` events it accepted, which
are kept apart from the events. Standard library only, so any SDK's CI can run it.
Stop it by killing the process: a ``hang`` request never ends on its own.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODES = ("ok", "slow", "500", "429", "400", "hang", "disconnect")


class _Collector(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str, delay_ms: int) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.delay = delay_ms / 1000
        self.accepted: set[int] = set()
        self.requests = 0
        self.reports = 0
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server: _Collector

    def do_GET(self) -> None:
        if self.path != "/_received":
            self._answer(404)
            return
        with self.server.lock:
            body = {
                "accepted": sorted(self.server.accepted),
                "requests": self.server.requests,
                "reports": self.server.reports,
            }
        self._answer(200, json.dumps(body).encode())

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path != "/v0/events":
            self._answer(404)
            return
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._answer(401)
            return
        with self.server.lock:
            self.server.requests += 1
        mode = self.server.mode
        if mode == "hang":
            threading.Event().wait()
        if mode == "disconnect":
            self.close_connection = True
            return
        if mode in ("500", "429", "400"):
            self._answer(int(mode))
            return
        if mode == "slow":
            time.sleep(self.server.delay)
        try:
            event = json.loads(body)
            sequence = event["sequence_number"]
        except (ValueError, KeyError, TypeError):
            self._answer(400)
            return
        with self.server.lock:
            if event.get("event_type") == "client_report":
                self.server.reports += 1
            else:
                self.server.accepted.add(sequence)
        self._answer(202)

    def _answer(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--delay-ms", type=int, default=0)
    args = parser.parse_args()
    server = _Collector(args.mode, args.delay_ms)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
