"""Self-check: each fake_collector.py mode answers as scenarios.json assumes,
and every mode scenarios.json uses is one the collector has."""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import time
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
}


def _post(port: int, path: str = "/v0/events", key: bool = True) -> tuple[int | str, float]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    headers = {"Authorization": "Bearer k"} if key else {}
    started = time.monotonic()
    try:
        connection.request("POST", path, json.dumps({"sequence_number": 7}), headers)
        return connection.getresponse().status, time.monotonic() - started
    except socket.timeout:
        return "timeout", time.monotonic() - started
    except (http.client.RemoteDisconnected, ConnectionError):
        return "closed", time.monotonic() - started
    finally:
        connection.close()


def _received(port: int) -> list[int]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    connection.request("GET", "/_received")
    accepted: list[int] = json.load(connection.getresponse())["accepted"]
    return accepted


def main() -> int:
    failed = False
    scenario_modes = {s["mode"] for s in json.loads((ROOT / "scenarios.json").read_text())["scenarios"]}
    if unknown := scenario_modes - set(MODES) - {"down"}:
        print(f"FAIL scenarios.json uses modes the collector lacks: {sorted(unknown)}")
        failed = True
    if set(EXPECTED) != set(MODES):
        print(f"FAIL this check covers {sorted(EXPECTED)}, the collector has {sorted(MODES)}")
        failed = True
    for mode, (want, least) in EXPECTED.items():
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "fake_collector.py"), "--mode", mode, "--delay-ms", str(DELAY_MS)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            port = int(process.stdout.readline())
            wrong_path, _ = _post(port, path="/v1/events")
            no_key, _ = _post(port, key=False)
            got, took = _post(port)
            accepted = _received(port)
        finally:
            process.kill()
            process.wait()
        want_accepted = [7] if want == 202 else []
        ok = (got, wrong_path, no_key, accepted) == (want, 404, 401, want_accepted) and took >= least
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {mode}: {got} in {took:.2f}s, accepted {accepted}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
