"""Report the spread of repeated query timings from a benchmark archive."""

import argparse
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from analyze_results import cell
from inspect_plans import DEFAULT_ARCHIVE, read_archive


@dataclass(frozen=True)
class Timing:
    query: str
    mode: str
    samples: tuple[float, ...]
    status: str

    def relative_mad(self) -> float | None:
        """Return median absolute deviation as a percentage of the median."""
        if self.status != "Complete" or len(self.samples) < 2:
            return None
        center = median(self.samples)
        if center == 0:
            return None
        return 100 * (median(abs(value - center) for value in self.samples) / center)


def collect_timings(records: dict, repetitions: int, mode_filter: str | None) -> list[Timing]:
    timings = []
    for query, arms in records.items():
        for mode, row in arms.items():
            if mode_filter and mode != mode_filter:
                continue
            samples = row.get("ms")
            if not isinstance(samples, list) or len(samples) > repetitions:
                raise ValueError(f"invalid timing sample list for {query}/{mode}")
            if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
                   for value in samples):
                raise ValueError(f"invalid runtime sample for {query}/{mode}")
            if row.get("error"):
                status = "Failed with samples" if samples else "Failed"
            # The demo's extended-statistics mode only records an EXPLAIN.
            elif repetitions == 0 or (mode == "statistics" and not samples):
                status = "Untimed"
            elif len(samples) != repetitions:
                status = "Incomplete"
            else:
                status = "Complete"
            timings.append(Timing(query, mode, tuple(samples), status))
    if not timings:
        raise ValueError(f"unknown mode: {mode_filter}" if mode_filter else "archive has no measurements")
    return timings


def render_profile(source: Path, metadata: dict, records: dict, mode: str | None) -> str:
    repetitions = metadata.get("repetitions")
    if type(repetitions) is not int or repetitions < 0:
        raise ValueError("archive metadata must contain a nonnegative repetition count")
    timings = collect_timings(records, repetitions, mode)
    scored = [(score, timing) for timing in timings if (score := timing.relative_mad()) is not None]
    scored.sort(key=lambda item: (-item[0], item[1].query, item[1].mode))
    ranked = {(timing.query, timing.mode) for _, timing in scored}

    lines = [
        "# Benchmark timing variability",
        "",
        f"Archive `{cell(source.name)}` specifies {repetitions} timing repetitions.",
        "Relative MAD measures the median absolute deviation from the median, divided "
        "by the median and expressed as a percentage. Only complete measurements with "
        "two or more samples and a positive median are ranked.",
        "",
        "## Sample coverage",
        "",
        "| Mode | Complete | Incomplete | Failed with samples | Failed | Untimed |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    status_counts: dict[str, Counter] = {}
    statuses = ("Complete", "Incomplete", "Failed with samples", "Failed", "Untimed")
    for timing in timings:
        status_counts.setdefault(timing.mode, Counter())[timing.status] += 1
    for label, counts in status_counts.items():
        fields = [cell(label), *(str(counts[status]) for status in statuses)]
        lines.append("| " + " | ".join(fields) + " |")

    lines.extend(["", "## Timing samples", ""])
    if repetitions == 0:
        lines.append("This run has no repeated timing samples.")
    else:
        lines.extend([
            "Ranked rows appear from highest to lowest relative MAD. Samples are listed "
            "in their recorded order. Times are in milliseconds.",
            "",
            "| Query | Mode | Status | Samples | Minimum | Median | Maximum | Relative MAD |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
        ])
        ordered = scored + [(None, timing) for timing in timings
                            if (timing.query, timing.mode) not in ranked]
        for score, timing in ordered:
            samples = timing.samples
            fields = [
                cell(timing.query),
                cell(timing.mode),
                timing.status,
                ", ".join(f"{value:.3f}" for value in samples) or "-",
                f"{min(samples):.3f}" if samples else "-",
                f"{median(samples):.3f}" if samples else "-",
                f"{max(samples):.3f}" if samples else "-",
                f"{score:.2f}%" if score is not None else "-",
            ]
            lines.append("| " + " | ".join(fields) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=DEFAULT_ARCHIVE,
                        help="benchmark JSONL or JSONL.GZ (default: corrected detailed run)")
    parser.add_argument("--mode", help="display a single benchmark mode")
    parser.add_argument("-o", "--output", type=Path, help="write Markdown to this path")
    args = parser.parse_args()

    try:
        if args.output and args.output.resolve() == args.archive.resolve():
            raise ValueError("report output must be a different file from the input archive")
        metadata, records = read_archive(args.archive)
        report = render_profile(args.archive, metadata, records, args.mode)
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except (OSError, EOFError, ValueError, OverflowError) as exc:
        print(f"{args.archive}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
