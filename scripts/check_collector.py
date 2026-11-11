"""Self-check: each fake_collector.py mode answers as scenarios.json and
shutdown.json assume, and every mode, step, transport and grouping the
scenario files and bench/bench.json use is one the collector and the runners know."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
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
    "no_batch": (202, 0),
    "split": (202, 0),
    "413": (413, 0),
}
# What a batch of three events gets: a status, and each event's result on a 200.
BATCH: dict[str, tuple[int | str, list[str]]] = {
    "ok": (200, ["accepted"] * 3),
    "slow": (200, ["accepted"] * 3),
    "500": (500, []),
    "429": (429, []),
    "400": (400, []),
    "hang": ("timeout", []),
    "disconnect": ("closed", []),
    "refuse_first": (200, ["refused", "accepted", "accepted"]),
    "no_batch": (404, []),
    "split": (413, []),
    "413": (413, []),
}
# What the next events get, for a mode whose answer changes.
THEN: dict[str, list[int]] = {"refuse_first": [202, 202]}


def _request(port: int, path: str, body: object, key: bool = True) -> tuple[int | str, float, dict[str, str], bytes]:
    """A POST's status (or "timeout" or "closed"), how long it took, its headers and its body."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    headers = {"Authorization": "Bearer k"} if key else {}
    started = time.monotonic()
    try:
        connection.request("POST", path, json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, time.monotonic() - started, dict(response.getheaders()), response.read()
    except socket.timeout:
        return "timeout", time.monotonic() - started, {}, b""
    except (http.client.RemoteDisconnected, ConnectionError):
        return "closed", time.monotonic() - started, {}, b""
    finally:
        connection.close()


def _post(
    port: int,
    path: str = "/v0/events",
    key: bool = True,
    event_type: str = "tool_call_start",
    sequence: int = 7,
) -> tuple[int | str, float]:
    status, took, _, _ = _request(port, path, {"event_type": event_type, "sequence_number": sequence}, key)
    return status, took


def _post_batch(port: int, body: object) -> tuple[int | str, list[str]]:
    """A batch's status, and each event's result on a 200."""
    status, _, headers, data = _request(port, "/v0/events/batch", body)
    if status == 429 and headers.get("Retry-After") != "1":
        return "429 without Retry-After: 1", []
    if status != 200:
        return status, []
    results = json.loads(data)["results"]
    ids = [r["event_id"] for r in results]
    sent = [e.get("event_id") for e in body["events"]] if isinstance(body, dict) else []
    refused_have_errors = all(("error" in r) == (r["status"] == "refused") for r in results)
    return (status if ids == sent and refused_have_errors else "bad results"), [r["status"] for r in results]


def _batch_of(*sequences: int) -> dict[str, list[dict[str, object]]]:
    return {"events": [{"event_type": "tool_call_start", "sequence_number": n, "event_id": f"e{n}"} for n in sequences]}


def _batch_answers(port: int, mode: str) -> bool:
    """A batch gets BATCH's answer, a resent event is a duplicate, and a bad
    batch is refused. Run on a fresh collector."""
    want = BATCH[mode]
    got = _post_batch(port, _batch_of(20, 21, 22))
    ok = got == want
    extra = ""
    if mode == "ok":
        resent = _post_batch(port, _batch_of(22, 23))
        bad = [_post_batch(port, body)[0] for body in ({"events": []}, _batch_of(*range(101)), [1], {"events": [{}]})]
        before = _received(port, "?lists=0")[0]["counts"].get("tool_call_start", 0)
        shared = _post_batch(port, {"events": [{"event_type": "tool_call_start", "sequence_number": 40, "event_id": i} for i in ("a", "b")]})
        added = _received(port, "?lists=0")[0]["counts"].get("tool_call_start", 0) - before
        ok = ok and resent == (200, ["duplicate", "accepted"]) and shared == (200, ["accepted"] * 2) and added == 2 and bad == [400] * 4
        extra = f", resent {resent}, two sessions' sequence 40 {shared} counted {added}, bad {bad}"
    if mode == "split":
        two = _post_batch(port, _batch_of(30, 31))
        ok = ok and two == (200, ["accepted"] * 2)
        extra = f", two events {two}"
    print(f"{'ok  ' if ok else 'FAIL'} {mode} batch: {got}{extra}")
    return ok


def _received(port: int, query: str = "") -> tuple[dict[str, Any], float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    started = time.monotonic()
    connection.request("GET", f"/_received{query}")
    return json.load(connection.getresponse()), time.monotonic() - started


def _woken(port: int, query: str, then: Callable[[], object]) -> float:
    """How long a GET waiting on ``query`` takes when ``then`` runs 0.1s in."""
    took: list[float] = []

    def wait() -> None:
        try:
            took.append(_received(port, query)[1])
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
    _, unmet = _received(port, "?accepted=9&wait_ms=300")
    _, met = _received(port, "?accepted=1&reports=1&wait_ms=2000")
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


def _counts_once(port: int) -> bool:
    """A resent sequence number is one event in ``counts``."""
    before = _received(port, "?lists=0")[0]["counts"]["tool_call_start"]
    _post(port, sequence=99)
    _post(port, sequence=99)
    after = _received(port, "?lists=0")[0]["counts"]["tool_call_start"]
    ok = after == before + 1
    print(f"{'ok  ' if ok else 'FAIL'} counts: a resent event adds {after - before}")
    return ok


STEPS = {"call", "flush", "close", "close_twice", "check"}
TRANSPORTS = {"memory", "stateful_http", "stateless_http"}
GROUPINGS = {"one", "per_client", "per_call"}
REASONS = {"buffer_full", "refused", "encode_failed", "write_failed", "after_close", "unsent_at_close"}


def _known_words() -> bool:
    files = {name: json.loads((ROOT / name).read_text())["scenarios"] for name in ("scenarios.json", "shutdown.json", "sessions.json")}
    bench = json.loads((ROOT.parent / "bench" / "bench.json").read_text())["measures"]
    checks = [
        ("scenarios.json", "modes the collector lacks", {s["mode"] for s in files["scenarios.json"]}, {*MODES, "down"}),
        ("shutdown.json", "modes the collector lacks", {s["mode"] for s in files["shutdown.json"]}, set(MODES)),
        ("shutdown.json", "steps the runners lack", {k for s in files["shutdown.json"] for step in s["steps"] for k in step}, STEPS),
        ("shutdown.json", "drop reasons SPEC lacks", {r for s in files["shutdown.json"] for r in s["expect"]["dropped"]}, REASONS),
        ("sessions.json", "transports the runners lack", {s["transport"] for s in files["sessions.json"]}, TRANSPORTS),
        ("sessions.json", "groupings the runners lack", {s["expect"]["sessions"] for s in files["sessions.json"]}, GROUPINGS),
        ("bench.json", "modes the collector lacks", {m["collector"] for m in bench}, {*MODES, "down"}),
    ]
    ok = True
    for name, what, used, known in checks:
        if unknown := used - known:
            print(f"FAIL {name} uses {what}: {sorted(unknown)}")
            ok = False
    return ok


def _with_collector(mode: str, check: Callable[[int, str], bool], *flags: str) -> bool:
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "fake_collector.py"), "--mode", mode, "--delay-ms", str(DELAY_MS), *flags],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        return check(int(process.stdout.readline()), mode)
    finally:
        process.kill()
        process.wait()


def _single_answers(port: int, mode: str) -> bool:
    """One event at a time gets EXPECTED's and THEN's answers. Run on a fresh collector."""
    want, least = EXPECTED[mode]
    then = THEN.get(mode, [])
    accepting = 202 in (want, *then)
    extra_ok = True
    wrong_path, _ = _post(port, path="/v1/events")
    no_key, _ = _post(port, key=False)
    if accepting:
        _post(port, event_type="client_report", sequence=1)
    got, took = _post(port)
    got_then = [_post(port, sequence=8 + i)[0] for i in range(len(then))]
    if accepting:
        _post(port, event_type="client_report", sequence=2)
    received, _ = _received(port)
    accepted, events, reports = received["accepted"], received["events"], received["reports"]
    counted, _ = _received(port, "?lists=0")
    if mode == "ok":
        extra_ok = _waits(port) and _counts_once(port)
    want_accepted = [7] if want == 202 else []
    want_accepted += [8 + i for i, status in enumerate(then) if status == 202]
    sent = [(e["event_type"], e["sequence_number"]) for e in events]
    ok = (got, got_then, wrong_path, no_key, accepted, sent, reports, counted) == (
        want,
        then,
        404,
        401,
        want_accepted,
        [("tool_call_start", n) for n in want_accepted],
        1 if accepting else 0,
        {
            "counts": {"tool_call_start": len(want_accepted)} if want_accepted else {},
            "requests": received["requests"],
            "reports": received["reports"],
        },
    ) and took >= least
    print(f"{'ok  ' if ok else 'FAIL'} {mode}: {got} then {got_then} in {took:.2f}s, accepted {accepted}, reports {reports}")
    return ok and extra_ok


def _keeps_no_events(port: int, mode: str) -> bool:
    """With --no-events, a batch and a resend are counted as without it, and
    'events' is left out."""
    got = _post_batch(port, _batch_of(20, 21))
    resent = _post_batch(port, _batch_of(21))
    received, _ = _received(port)
    ok = (got, resent, received["counts"], received["accepted"], "events" in received) == (
        (200, ["accepted"] * 2),
        (200, ["duplicate"]),
        {"tool_call_start": 2},
        [20, 21],
        False,
    )
    print(f"{'ok  ' if ok else 'FAIL'} {mode} --no-events: {got} then {resent}, received {received}")
    return ok


SETTINGS = {"version": 1, "audiences": [{"id": "everyone", "match": {}, "capture": {}}]}


def _settings(port: int) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/v0/settings?server=v")
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    return response.status, json.loads(body) if response.status == 200 else None


def _serves_settings(port: int, mode: str) -> bool:
    """With --settings, GET /v0/settings answers the document in any mode."""
    got = _settings(port)
    ok = got == (200, SETTINGS)
    print(f"{'ok  ' if ok else 'FAIL'} {mode} --settings: {got[0]}")
    return ok


def _no_settings(port: int, mode: str) -> bool:
    """Without --settings, GET /v0/settings is a 404, as the SDKs' collector tests expect."""
    got = _settings(port)[0]
    ok = got == 404
    print(f"{'ok  ' if ok else 'FAIL'} {mode} without --settings: {got}")
    return ok and _keeps_no_events(port, mode)


def main() -> int:
    failed = not _known_words()
    if set(EXPECTED) != set(MODES) or set(BATCH) != set(MODES):
        print(f"FAIL this check covers {sorted(EXPECTED)} and {sorted(BATCH)}, the collector has {sorted(MODES)}")
        failed = True
    for mode in EXPECTED:
        failed |= not _with_collector(mode, _batch_answers)
        failed |= not _with_collector(mode, _single_answers)
    failed |= not _with_collector("ok", _no_settings, "--no-events")
    failed |= not _with_collector("hang", _serves_settings, "--settings", json.dumps(SETTINGS))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
