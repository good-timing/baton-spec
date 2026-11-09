"""Prints a Markdown table comparing speed and load runs of one SDK.

    python compare_bench.py RESULT.json [RESULT.json ...]

Each file is one run, shaped as bench/bench.json's 'result' says. Runs are
grouped by version and source; the first group is the one from the package
registry when there is one. For each metric it prints each group's median and
range over its runs, and the change between the medians: a percentage, or
the difference where the old range reaches zero. A change whose new median
lies inside the old group's range is marked as noise. It decides
nothing: it exits non-zero only when a file does not have the shape
bench.json gives. Standard library only.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BENCH = json.loads((Path(__file__).resolve().parent.parent / "bench" / "bench.json").read_text())
METRICS = {m["name"]: m["metrics"] for m in BENCH["measures"]}
KEYS = set(BENCH["result"]) - {"cpu"}


def _shape_errors(name: str, run: dict[str, Any]) -> list[str]:
    missing = KEYS - set(run)
    errors = [f"{name}: lacks {sorted(missing)}"] if missing else []
    measures = run.get("measures", {})
    if extra := set(measures) - set(METRICS):
        errors.append(f"{name}: has measures bench.json lacks: {sorted(extra)}")
    for measure, metrics in METRICS.items():
        got = measures.get(measure, {})
        if set(got) != set(metrics) or not all(isinstance(v, (int, float)) for v in got.values()):
            errors.append(f"{name}: {measure} has {sorted(got)}, bench.json has {sorted(metrics)}, all numbers")
    return errors


def _figure(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def _change(old: list[float], new: list[float]) -> str:
    before, after = statistics.median(old), statistics.median(new)
    if before == after:
        return "same"
    noise = " (noise)" if min(old) <= after <= max(old) else ""
    if min(old) <= 0 <= max(old):
        return f"{after - before:+.3g}{noise}"
    return f"{(after - before) / abs(before):+.0%}{noise}"


def cell(values: list[float]) -> str:
    return f"{_figure(statistics.median(values))} ({_figure(min(values))}–{_figure(max(values))})"


def cpus(group: list[dict[str, Any]]) -> str:
    counts = Counter(str(r.get("cpu", "unknown")) for r in group)
    return ", ".join(f"{cpu} ({n})" for cpu, n in counts.most_common())


def table(runs: list[dict[str, Any]]) -> str:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in sorted(runs, key=lambda r: r["source"] != "published"):
        groups.setdefault((run["version"], run["source"]), []).append(run)
    labels = [f"{version} {source}" for version, source in groups]
    pair = len(groups) == 2
    sdks = sorted({r["sdk"] for r in runs})
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
    rows = [(m, metric) for m, metrics in METRICS.items() for metric in metrics] + [("process", "max_rss_bytes")]
    for measure, metric in rows:
        values = [
            [float(r["max_rss_bytes"] if measure == "process" else r["measures"][measure][metric]) for r in group]
            for group in groups.values()
        ]
        change = f" {_change(*values)} |" if pair else ""
        lines.append(f"| {measure} | {metric} | " + " | ".join(cell(v) for v in values) + " |" + change)
    return "\n".join(lines)


def main() -> int:
    runs, errors = [], []
    for name in sys.argv[1:]:
        run = json.loads(Path(name).read_text())
        errors += _shape_errors(name, run)
        runs.append(run)
    if errors or not runs:
        print("\n".join(errors) or "no result files given", file=sys.stderr)
        return 1
    print(table(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
