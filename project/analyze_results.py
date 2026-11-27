"""Summarize a saved bench.py CSV without starting PostgreSQL."""

import argparse
import csv
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import geometric_mean, median


JOB_RESULTS = Path(__file__).resolve().parents[1] / "results" / "job"
DEFAULT_SOURCE = JOB_RESULTS / "corrected" / "job-detailed.csv"
BENCHMARK_ARMS = ("baseline", "disabled", "collect", "learned", "statistics")
REQUIRED_COLUMNS = {
    "query", "arm", "median_ms", "execution_ms", "qerror", "applied",
    "result_hash", "plan", "error",
}
TIMING_COLUMNS = {"median": "median_ms", "explain": "execution_ms"}
STATUS_LABELS = {
    "missing": "Missing",
    "complete": "Completed",
    "timeout": "Timeout",
    "error": "Error",
}


def number(value: str) -> float | None:
    if not value.strip():
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"expected a finite, nonnegative value, got {value!r}")
    return parsed


@dataclass(frozen=True)
class Measurement:
    elapsed: float | None
    qerror: float | None
    applied: int | None
    result_hash: str
    plan: str
    error: str

    @property
    def status(self) -> str:
        if not self.error:
            return "complete"
        if re.search(r"timeout|timed out", self.error, re.IGNORECASE):
            return "timeout"
        return "error"


def load_results(path: Path, timing: str) -> dict[str, dict[str, Measurement]]:
    results: dict[str, dict[str, Measurement]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"missing CSV columns: {', '.join(sorted(missing))}")

        for row in reader:
            try:
                if None in row or any(row[column] is None for column in REQUIRED_COLUMNS):
                    raise ValueError("incorrect number of CSV fields")
                query, arm = row["query"].strip(), row["arm"].strip()
                if not query or not arm:
                    raise ValueError("query and arm are required")

                measurements = results.setdefault(query, {})
                if arm in measurements:
                    raise ValueError(f"duplicate measurement for {query}/{arm}")

                applied = row["applied"].strip()
                if applied and (not applied.isascii() or not applied.isdecimal()):
                    raise ValueError(f"invalid feedback count {applied!r}")
                measurements[arm] = Measurement(
                    elapsed=number(row[TIMING_COLUMNS[timing]]),
                    qerror=number(row["qerror"]),
                    applied=int(applied) if applied else None,
                    result_hash=row["result_hash"].strip(),
                    plan=row["plan"].strip(),
                    error=row["error"].strip(),
                )
            except ValueError as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc

    if not results:
        raise ValueError("CSV has no benchmark rows")
    return results


def completed_ratio(reference: Measurement | None, candidate: Measurement | None,
                    field: str) -> float | None:
    if reference is None or candidate is None:
        return None
    if reference.status != "complete" or candidate.status != "complete":
        return None
    before, after = getattr(reference, field), getattr(candidate, field)
    if before is None or after is None or before <= 0 or after <= 0:
        return None
    return before / after


def result_check(measurements: dict[str, Measurement]) -> str:
    hashes = [measurement.result_hash for measurement in measurements.values()
              if measurement.result_hash]
    if len(set(hashes)) > 1:
        return "Mismatch"
    if len(hashes) >= 2:
        return "Match"
    return "Insufficient hashes"


def display_number(value: float | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:.3g}{suffix}"


def display_time(measurement: Measurement | None) -> str:
    if measurement is None:
        return STATUS_LABELS["missing"]
    if measurement.status != "complete":
        return STATUS_LABELS[measurement.status]
    if measurement.elapsed is None:
        return "Untimed"
    return display_number(measurement.elapsed, " ms")


def cell(value: str) -> str:
    return " ".join(value.split()).replace("\\", "\\\\").replace("|", "\\|")


def render_report(source: Path, results: dict[str, dict[str, Measurement]],
                  timing: str) -> str:
    arms_present = {arm for measurements in results.values() for arm in measurements}
    arms = [arm for arm in BENCHMARK_ARMS if arm in arms_present]
    arms.extend(sorted(arms_present.difference(BENCHMARK_ARMS)))
    checks = {query: result_check(measurements) for query, measurements in results.items()}
    check_counts = Counter(checks.values())
    timing_label = {
        "median": "median of repeated client timings",
        "explain": "server execution time from EXPLAIN ANALYZE",
    }[timing]
    lines = [
        "# PostgreSQL cardinality feedback benchmark",
        "",
        f"Analyzed {len(results)} queries from `{source.name}` using {timing_label}.",
        f"Saved result hashes agree for {check_counts['Match']} queries, differ for "
        f"{check_counts['Mismatch']}, and are insufficient for {check_counts['Insufficient hashes']}.",
        "",
        "## Completion by mode",
        "",
        "| Mode | Completed | Untimed among completed | Timeout | Error | Missing |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm in arms:
        rows = [measurements.get(arm) for measurements in results.values()]
        counts = Counter(row.status if row is not None else "missing" for row in rows)
        untimed = sum(row is not None and row.status == "complete" and row.elapsed is None
                      for row in rows)
        lines.append(f"| {cell(arm)} | {counts['complete']} | {untimed} | {counts['timeout']} | "
                     f"{counts['error']} | {counts['missing']} |")

    if "baseline" in arms:
        lines.extend([
            "",
            "## Paired comparison with stock PostgreSQL",
            "",
            "Speedup is stock time divided by mode time. Q-error improvement is stock "
            "q-error divided by mode q-error. Both calculations use completed pairs with "
            "positive values. A ratio above 1 favors the mode.",
            "",
            "| Mode | Timed pairs | Faster | Slower | Tied | Geometric mean speedup | "
            "Q-error pairs | Median q-error improvement |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for arm in arms:
            if arm == "baseline":
                continue
            speedups = []
            improvements = []
            for measurements in results.values():
                stock, other = measurements.get("baseline"), measurements.get(arm)
                speedup = completed_ratio(stock, other, "elapsed")
                improvement = completed_ratio(stock, other, "qerror")
                if speedup is not None:
                    speedups.append(speedup)
                if improvement is not None:
                    improvements.append(improvement)
            faster = sum(ratio > 1 for ratio in speedups)
            slower = sum(ratio < 1 for ratio in speedups)
            tied = len(speedups) - faster - slower
            mean_speed = geometric_mean(speedups) if speedups else None
            typical_improvement = median(improvements) if improvements else None
            lines.append(f"| {cell(arm)} | {len(speedups)} | {faster} | {slower} | {tied} | "
                         f"{display_number(mean_speed, 'x')} | {len(improvements)} | "
                         f"{display_number(typical_improvement, 'x')} |")

    lines.extend([
        "",
        "## Query details",
        "",
        "The plan comparison uses the saved stock and learned plan shapes. "
        "Feedback applications come from the learned planning trace.",
        "",
        "| Query | " + " | ".join(cell(arm) for arm in arms) +
        " | Stock / learned time | Stock q-error | Learned q-error | Feedback applications | "
        "Plan shape | Result hashes |",
        "| --- | " + " | ".join("---:" for _ in arms) +
        " | ---: | ---: | ---: | ---: | --- | --- |",
    ])
    for query, measurements in results.items():
        stock, learned = measurements.get("baseline"), measurements.get("learned")
        speedup = completed_ratio(stock, learned, "elapsed")
        plan = "-"
        if stock and learned and stock.plan and learned.plan:
            plan = "Changed" if stock.plan != learned.plan else "Same"
        fields = [
            cell(query),
            *(display_time(measurements.get(arm)) for arm in arms),
            display_number(speedup, "x"),
            display_number(stock.qerror if stock else None),
            display_number(learned.qerror if learned else None),
            str(learned.applied) if learned and learned.applied is not None else "-",
            plan,
            checks[query],
        ]
        lines.append("| " + " | ".join(fields) + " |")

    failures = [(query, arm, measurement.error) for query, measurements in results.items()
                for arm, measurement in measurements.items() if measurement.error]
    if failures:
        lines.extend(["", "## Timeouts and errors", "", "| Query | Mode | Message |",
                      "| --- | --- | --- |"])
        lines.extend(f"| {cell(query)} | {cell(arm)} | {cell(error)} |"
                     for query, arm, error in failures)
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=DEFAULT_SOURCE,
                        help="benchmark CSV (defaults to the saved corrected detailed run)")
    parser.add_argument("--timing", choices=TIMING_COLUMNS, default="median",
                        help="timing to compare (default: median)")
    parser.add_argument("-o", "--output", type=Path,
                        help="write Markdown to this path (default: standard output)")
    args = parser.parse_args()

    try:
        if args.output and args.output.resolve() == args.source.resolve():
            raise ValueError("report output must be a different file from the input CSV")
        results = load_results(args.source, args.timing)
        report = render_report(args.source, results, args.timing)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
