"""A collector that fails on purpose, for each SDK's collector tests and speed runs.

    python fake_collector.py --mode MODE [--delay-ms N]

Prints the port it listens on as the first line of stdout. ``POST
/v0/events`` and ``POST /v0/events/batch`` (SPEC §2.5) with a bearer key
answer by mode; any other POST gets 404 or 401. ``GET /_received`` returns
the sequence numbers it accepted, every event it accepted in the order it
came (a resent event_id once), how many it accepted of each event type (a
resent event once, by event_id or else sequence number), how many requests it
got, and how many ``client_report`` events it accepted after an event, which
are kept apart from the events. With ``?accepted=N&reports=M&wait_ms=T`` it
first waits up to T ms until it has that many of each. ``?lists=0`` leaves
out the sequence numbers and the events. ``refuse_first`` refuses the first
event and accepts the rest. ``no_batch`` is a collector without the batch
endpoint. ``split`` answers 413 to a batch of more than 2 events, and ``413``
to every request. ``429`` asks for a 1 s wait with ``Retry-After``. Standard
library only, so any SDK's CI can run it. Stop it by killing the process: a
``hang`` request never ends on its own.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MODES = ("ok", "slow", "500", "429", "400", "hang", "disconnect", "refuse_first", "no_batch", "split", "413")
BATCH_CAP = 100


class _Collector(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode: str, delay_ms: int) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode = mode
        self.delay = delay_ms / 1000
        self.accepted: set[int] = set()
        self.event_ids: set[object] = set()
        self.events: list[dict[str, object]] = []
        self.counts: Counter[str] = Counter()
        self.requests = 0
        self.reports = 0
        self.refused_first = False
        self.changed = threading.Condition()


class _Handler(BaseHTTPRequestHandler):
    server: _Collector

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/_received":
            self._answer(404)
            return
        try:
            query = {k: int(v[0]) for k, v in parse_qs(url.query).items()}
        except ValueError:
            self._answer(400)
            return
        with self.server.changed:
            self.server.changed.wait_for(
                lambda: len(self.server.accepted) >= query.get("accepted", 0)
                and self.server.reports >= query.get("reports", 0),
                timeout=query.get("wait_ms", 0) / 1000,
            )
            body: dict[str, object] = {
                "counts": dict(self.server.counts),
                "requests": self.server.requests,
                "reports": self.server.reports,
            }
            if query.get("lists", 1):
                body["accepted"] = sorted(self.server.accepted)
                body["events"] = self.server.events
        self._answer(200, json.dumps(body).encode())

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        mode = self.server.mode
        batch = self.path == "/v0/events/batch"
        known = self.path == "/v0/events" or (batch and mode != "no_batch")
        if not known:
            self._answer(404)
            return
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            self._answer(401)
            return
        with self.server.changed:
            self.server.requests += 1
        if mode == "hang":
            threading.Event().wait()
        if mode == "disconnect":
            self.close_connection = True
            return
        if mode == "429":
            self._answer(429, headers={"Retry-After": "1"})
            return
        if mode in ("500", "400", "413"):
            self._answer(int(mode))
            return
        if mode == "slow":
            time.sleep(self.server.delay)
        try:
            parsed = json.loads(body)
            events = parsed["events"] if batch else [parsed]
            if not isinstance(events, list) or not 0 < len(events) <= BATCH_CAP:
                raise ValueError
            if not all(isinstance(e, dict) and "sequence_number" in e for e in events):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self._answer(400)
            return
        if batch and mode == "split" and len(events) > 2:
            self._answer(413)
            return
        with self.server.changed:
            statuses = [self._take(event) for event in events]
            self.server.changed.notify_all()
        if not batch:
            self._answer(400 if statuses[0] == "refused" else 202)
            return
        results = [
            {"event_id": event.get("event_id"), "status": status}
            | ({"error": {"code": "payload_malformed", "message": "refused on purpose"}} if status == "refused" else {})
            for event, status in zip(events, statuses)
        ]
        self._answer(200, json.dumps({"results": results}).encode())

    def _take(self, event: dict[str, object]) -> str:
        """Accept, or refuse in mode ``refuse_first``, one event. Call holding ``changed``."""
        server = self.server
        report = event.get("event_type") == "client_report"
        if server.mode == "refuse_first" and not report and not server.refused_first:
            server.refused_first = True
            return "refused"
        # A report sent before any delivery breaks SPEC §11.4.7, so it is not counted.
        if report:
            if server.accepted:
                server.reports += 1
            return "accepted"
        # Sessions share sequence numbers, so only event_id marks a resend (SPEC §2.4).
        event_id = event.get("event_id")
        if event_id is not None:
            if event_id in server.event_ids:
                return "duplicate"
            server.event_ids.add(event_id)
        sequence = event["sequence_number"]
        if event_id is not None or sequence not in server.accepted:
            server.counts[str(event.get("event_type"))] += 1
        server.accepted.add(sequence)
        server.events.append(event)
        return "accepted"

    def _answer(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
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
