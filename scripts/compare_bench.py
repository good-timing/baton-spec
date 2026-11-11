"""Prints a Markdown table comparing speed and load runs of one SDK.

    python compare_bench.py RESULT.json [RESULT.json ...]

Each file is one run, shaped as bench/bench.json's 'result' says. Runs are
grouped by version and source; the first group is the one from the package
registry when there is one. For each metric it prints each group's median and
range over its runs, and the change between the medians: a percentage, or
the difference where the old range reaches zero or the metric has a
'noise_floor'. A change whose new median lies inside the old group's range,
or that is smaller than the metric's noise floor for the SDK, is marked as
noise. A published version before a measure's 'since' lacks it; that
shows as a dash. It decides nothing: it exits non-zero only when a file does not
have the shape bench.json gives, some runs of one version have a measure and
others lack it, or bench.json's noise floors are malformed.
Standard library only.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BENCH = json.loads((Path(__file__).resolve().parent.parent / "bench" / "bench.json").read_text())
METRICS = {m["name"]: m["metrics"] for m in BENCH["measures"]}
SDKS = {"python", "typescript", "go"}
SINCE: dict[str, dict[str, str]] = {m["name"]: m.get("since", {}) for m in BENCH["measures"]}


def release(version: str) -> tuple[int, ...]:
    """The release numbers of any SDK's version: v1.2.0, 1.2.0-beta.1 and 1.2.0rc1 are (1, 2, 0)."""
    found = re.match(r"v?(\d+(?:\.\d+)*)", version)
    if found is None:
        raise ValueError(f"not a version: {version!r}")
    return tuple(int(part) for part in found.group(1).split("."))


def _runs(measure: str, sdk: object) -> bool:
    return not SINCE[measure] or sdk in SINCE[measure]


def _may_lack(measure: str, run: dict[str, Any]) -> bool:
    """A published version older than the measure's 'since' for its SDK."""
    since = SINCE[measure].get(str(run.get("sdk")))
    if run.get("source") != "published" or since is None:
        return False
    try:
        return release(str(run.get("version"))) < release(since)
    except ValueError:
        return False
KEYS = set(BENCH["result"]) - {"cpu"}


def _floors() -> tuple[dict[tuple[str, str], dict[str, float]], list[str]]:
    floors, errors = {}, []
    for m in BENCH["measures"]:
        given = m.get("noise_floor", {})
        if not isinstance(given, dict):
            errors.append(f"bench.json: {m['name']} noise_floor is not an object")
            continue
        for metric, by_sdk in given.items():
            numbers = isinstance(by_sdk, dict) and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in by_sdk.values()
            )
            if metric in m["metrics"] and numbers and set(by_sdk) == SDKS:
                floors[(m["name"], metric)] = by_sdk
            else:
                errors.append(
                    f"bench.json: {m['name']} noise_floor {metric!r} needs a metric it measures, "
                    f"with a number for each of {sorted(SDKS)}"
                )
    return floors, errors


FLOORS, FLOOR_ERRORS = _floors()


def _shape_errors(name: str, run: dict[str, Any]) -> list[str]:
    missing = KEYS - set(run)
    errors = [f"{name}: lacks {sorted(missing)}"] if missing else []
    if "sdk" in run and run["sdk"] not in SDKS:
        errors.append(f"{name}: sdk is {run['sdk']!r}, not one of {sorted(SDKS)}")
    measures = run.get("measures", {})
    if extra := set(measures) - set(METRICS):
        errors.append(f"{name}: has measures bench.json lacks: {sorted(extra)}")
    for measure, metrics in METRICS.items():
        runs = _runs(measure, run.get("sdk"))
        if measure not in measures:
            if runs and not _may_lack(measure, run):
                errors.append(f"{name}: lacks {measure}")
            continue
        if not runs:
            errors.append(f"{name}: has {measure}, which bench.json runs for {sorted(SINCE[measure])} only")
        got = measures[measure]
        if set(got) != set(metrics) or not all(isinstance(v, (int, float)) for v in got.values()):
            errors.append(f"{name}: {measure} has {sorted(got)}, bench.json has {sorted(metrics)}, all numbers")
    return errors


def _groups(runs: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in sorted(runs, key=lambda r: r["source"] != "published"):
        groups.setdefault((run["version"], run["source"]), []).append(run)
    return groups


def _partial_errors(groups: dict[tuple[str, str], list[dict[str, Any]]]) -> list[str]:
    """A measure that some runs of one version have and others lack."""
    return [
        f"{version} {source}: {measure} is in {have} of {len(group)} runs"
        for (version, source), group in groups.items()
        for measure in METRICS
        if 0 < (have := sum(measure in r["measures"] for r in group)) < len(group)
    ]


def _figure(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _change(old: list[float], new: list[float], floor: float | None) -> str:
    before, after = statistics.median(old), statistics.median(new)
    if before == after:
        return "same"
    under_floor = floor is not None and abs(after - before) < floor
    noise = " (noise)" if min(old) <= after <= max(old) or under_floor else ""
    if min(old) <= 0 <= max(old) or floor is not None:
        return ("+" if after > before else "") + _figure(after - before) + noise
    return f"{(after - before) / abs(before):+.0%}{noise}"


def cell(values: list[float]) -> str:
    return f"{_figure(statistics.median(values))} ({_figure(min(values))}–{_figure(max(values))})"


def cpus(group: list[dict[str, Any]]) -> str:
    counts = Counter(str(r.get("cpu", "unknown")) for r in group)
    return ", ".join(f"{cpu} ({n})" for cpu, n in counts.most_common())


def table(runs: list[dict[str, Any]]) -> str:
    groups = _groups(runs)
    labels = [f"{version} {source}" for version, source in groups]
    pair = len(groups) == 2
    sdks = sorted({r["sdk"] for r in runs})
    sdk = sdks[0] if len(sdks) == 1 else None
    lines = [
        f"## {', '.join(sdks)}: {' vs '.join(labels)}",
        "",
        f"{BENCH['bar']} Median (lowest–highest) of {', '.join(str(len(g)) for g in groups.values())} runs.",
        "",
        "CPU: " + "; ".join(f"{label}: {cpus(group)}" for label, group in zip(labels, groups.values())),
        "",
        "| measure | metric | " + " | ".join(labels) + (" | change |" if pair else ""),
        "|---|---|" + "---|" * len(groups) + ("---|" if pair else ""),
    ]
    measured = [m for m in METRICS if any(m in r["measures"] for r in runs)]
    rows = [(m, metric) for m in measured for metric in METRICS[m]] + [("process", "max_rss_bytes")]
    for measure, metric in rows:
        values = [
            [float(r["max_rss_bytes"] if measure == "process" else r["measures"][measure][metric]) for r in group]
            if measure == "process" or measure in group[0]["measures"]
            else []
            for group in groups.values()
        ]
        floor = FLOORS.get((measure, metric), {}).get(sdk)
        change = (f" {_change(*values, floor)} |" if all(values) else " — |") if pair else ""
        lines.append(f"| {measure} | {metric} | " + " | ".join(cell(v) if v else "—" for v in values) + " |" + change)
    return "\n".join(lines)


def main() -> int:
    runs: list[dict[str, Any]] = []
    errors = list(FLOOR_ERRORS)
    for name in sys.argv[1:]:
        run = json.loads(Path(name).read_text())
        errors += _shape_errors(name, run)
        runs.append(run)
    if not errors:
        errors += _partial_errors(_groups(runs))
    if errors or not runs:
        print("\n".join(errors) or "no result files given", file=sys.stderr)
        return 1
    print(table(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
