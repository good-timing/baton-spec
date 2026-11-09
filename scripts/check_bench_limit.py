"""Runs each measure that has 'limits' in bench/bench.json, 'repeats' times
alone, and checks each limited metric's median against its limit.

    python check_bench_limit.py RUNNER [ARG ...]

In each ARG, {measure} becomes the measure's name and {out} a result file.
Exits 0 when every median is under its limit, 1 when one is not, and 2 when
the runner or bench.json is broken.
"""

from __future__ import annotations

import json
import math
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from compare_bench import BENCH, cell, cpus


class Broken(Exception):
    pass


def run_alone(m: dict[str, Any], runner: list[str], work: str) -> list[dict[str, Any]]:
    runs = []
    for n in range(BENCH["repeats"]):
        out = str(Path(work) / f"{m['name']}-{n}.json")
        command = [arg.replace("{measure}", m["name"]).replace("{out}", out) for arg in runner]
        try:
            if subprocess.run(command, check=False).returncode != 0:
                raise Broken(f"{command} failed")
            run = json.loads(Path(out).read_text())
        except (OSError, ValueError) as e:
            raise Broken(f"{command}: {e}") from None
        runs.append(run)
    return runs


def number(run: Any, measure: str, metric: str) -> float:
    try:
        value = run["measures"][measure][metric]
    except (KeyError, TypeError):
        value = None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Broken(f"{measure} {metric} is {value!r} in a run, not a number")
    return float(value)


def under(m: dict[str, Any], runs: list[dict[str, Any]]) -> bool:
    held = True
    for metric, limit in m["limits"].items():
        values = [number(r, m["name"], metric) for r in runs]
        summary = (
            f"{m['name']} {metric}: {cell(values)} over {len(values)} runs, must be under {limit:g}. CPU: {cpus(runs)}."
        )
        if statistics.median(values) < limit:
            print(summary)
        else:
            held = False
            print(
                f"NOT UNDER THE LIMIT. {summary} Shared CI machines differ in speed, so rerun this job once "
                "before treating it as a slowdown.",
                file=sys.stderr,
            )
    return held


def main() -> int:
    runner = sys.argv[1:]
    if not runner:
        print(__doc__, file=sys.stderr)
        return 2
    limited = [m for m in BENCH["measures"] if m.get("limits")]
    try:
        for m in limited:
            if unknown := set(m["limits"]) - set(m["metrics"]):
                raise Broken(f"bench.json: {m['name']} limits {sorted(unknown)}, which it does not measure")
        with tempfile.TemporaryDirectory() as work:
            held = [under(m, run_alone(m, runner, work)) for m in limited]
    except Broken as e:
        print(f"BROKEN, NOT SLOW: {e}.", file=sys.stderr)
        return 2
    return 0 if all(held) else 1


if __name__ == "__main__":
    sys.exit(main())
