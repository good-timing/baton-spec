"""Self-check: each fake_collector.py mode answers as scenarios.json and
shutdown.json assume, and every mode, step, transport and grouping the
scenario files use is one the collector and the runners know."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "collector"
sys.path.insert(0, str(ROOT))
from fake_collector import MODES  # noqa: E402

DELAY_MS = 50
# What one POST gets: a status, "timeout" or "closed"; and the least time it takes.
EXPECTED: dict[str, tuple[int | str, float]] = {
    "ok": (202, 0),
    "slow": (202, DELAY_MS / 1000),
    "500": (500, 0),
    "429": (429, 0),
    "400": (400, 0),
    "hang": ("timeout", 0.5),
    "disconnect": ("closed", 0),
    "refuse_first": (400, 0),
}
# What the next events get, for a mode whose answer changes.
THEN: dict[str, list[int]] = {"refuse_first": [202, 202]}


def _post(
    port: int,
    path: str = "/v0/events",
    key: bool = True,
    event_type: str = "tool_call_start",
    sequence: int = 7,
) -> tuple[int | str, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    headers = {"Authorization": "Bearer k"} if key else {}
    started = time.monotonic()
    try:
        body = {"event_type": event_type, "sequence_number": sequence}
        connection.request("POST", path, json.dumps(body), headers)
        return connection.getresponse().status, time.monotonic() - started
    except socket.timeout:
        return "timeout", time.monotonic() - started
    except (http.client.RemoteDisconnected, ConnectionError):
        return "closed", time.monotonic() - started
    finally:
        connection.close()


def _received(port: int, query: str = "") -> tuple[list[int], int, float]:
    accepted, _, reports, took = _received_events(port, query)
    return accepted, reports, took


def _received_events(port: int, query: str = "") -> tuple[list[int], list[dict[str, object]], int, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    started = time.monotonic()
    connection.request("GET", f"/_received{query}")
    received = json.load(connection.getresponse())
    return received["accepted"], received["events"], received["reports"], time.monotonic() - started


def _woken(port: int, query: str, then: Callable[[], object]) -> float:
    """How long a GET waiting on ``query`` takes when ``then`` runs 0.1s in."""
    took: list[float] = []

    def wait() -> None:
        try:
            took.append(_received(port, query)[2])
        except OSError:
            pass

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.1)
    then()
    waiter.join()
    return took[0] if took else float("inf")


def _waits(port: int) -> bool:
    """``/_received`` waits for the events and reports it is asked for, and no longer.
    Run after one event and one report were accepted."""
    _, _, unmet = _received(port, "?accepted=9&wait_ms=300")
    _, _, met = _received(port, "?accepted=1&reports=1&wait_ms=2000")
    by_event = _woken(port, "?accepted=2&wait_ms=2000", lambda: _post(port, sequence=8))
    by_report = _woken(
        port, "?reports=2&wait_ms=2000", lambda: _post(port, event_type="client_report", sequence=3)
    )
    bad = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        bad.request("GET", "/_received?wait_ms=soon")
        bad_query: int | str = bad.getresponse().status
    except (http.client.HTTPException, OSError):
        bad_query = "closed"
    woken = [by_event, by_report]
    ok = unmet >= 0.3 and met < 1 and all(0.1 <= t < 1.5 for t in woken) and bad_query == 400
    print(
        f"{'ok  ' if ok else 'FAIL'} wait: unmet {unmet:.2f}s, met {met:.2f}s,"
        f" woken by an event {by_event:.2f}s, by a report {by_report:.2f}s, bad query {bad_query}"
    )
    return ok


STEPS = {"call", "flush", "close", "close_twice", "check"}
TRANSPORTS = {"memory", "stateful_http", "stateless_http"}
GROUPINGS = {"one", "per_client", "per_call"}
REASONS = {"buffer_full", "refused", "encode_failed", "write_failed", "after_close", "unsent_at_close"}


def _known_words() -> bool:
    ok = True
    for name in ("scenarios.json", "shutdown.json"):
        scenarios = json.loads((ROOT / name).read_text())["scenarios"]
        if unknown := {s["mode"] for s in scenarios} - set(MODES) - {"down"}:
            print(f"FAIL {name} uses modes the collector lacks: {sorted(unknown)}")
            ok = False
    shutdown = json.loads((ROOT / "shutdown.json").read_text())["scenarios"]
    if unknown := {key for s in shutdown for step in s["steps"] for key in step} - STEPS:
        print(f"FAIL shutdown.json uses steps the runners lack: {sorted(unknown)}")
        ok = False
    if unknown := {reason for s in shutdown for reason in s["expect"]["dropped"]} - REASONS:
        print(f"FAIL shutdown.json names drop reasons SPEC lacks: {sorted(unknown)}")
        ok = False
    sessions = json.loads((ROOT / "sessions.json").read_text())["scenarios"]
    if unknown := {s["transport"] for s in sessions} - TRANSPORTS:
        print(f"FAIL sessions.json uses transports the runners lack: {sorted(unknown)}")
        ok = False
    if unknown := {s["expect"]["sessions"] for s in sessions} - GROUPINGS:
        print(f"FAIL sessions.json uses groupings the runners lack: {sorted(unknown)}")
        ok = False
    return ok


def main() -> int:
    failed = not _known_words()
    if set(EXPECTED) != set(MODES):
        print(f"FAIL this check covers {sorted(EXPECTED)}, the collector has {sorted(MODES)}")
        failed = True
    for mode, (want, least) in EXPECTED.items():
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "fake_collector.py"), "--mode", mode, "--delay-ms", str(DELAY_MS)],
            stdout=subprocess.PIPE,
            text=True,
        )
        then = THEN.get(mode, [])
        accepting = 202 in (want, *then)
        try:
            assert process.stdout is not None
            port = int(process.stdout.readline())
            wrong_path, _ = _post(port, path="/v1/events")
            no_key, _ = _post(port, key=False)
            if accepting:
                _post(port, event_type="client_report", sequence=1)
            got, took = _post(port)
            got_then = [_post(port, sequence=8 + i)[0] for i in range(len(then))]
            if accepting:
                _post(port, event_type="client_report", sequence=2)
            accepted, events, reports, _ = _received_events(port)
            if mode == "ok":
                failed |= not _waits(port)
        finally:
            process.kill()
            process.wait()
        want_accepted = [7] if want == 202 else []
        want_accepted += [8 + i for i, status in enumerate(then) if status == 202]
        sent = [(e["event_type"], e["sequence_number"]) for e in events]
        ok = (got, got_then, wrong_path, no_key, accepted, sent, reports) == (
            want,
            then,
            404,
            401,
            want_accepted,
            [("tool_call_start", n) for n in want_accepted],
            1 if accepting else 0,
        ) and took >= least
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {mode}: {got} then {got_then} in {took:.2f}s, accepted {accepted}, reports {reports}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
