"""Inspect the PostgreSQL EXPLAIN trees stored in benchmark JSONL archives."""

import argparse
import gzip
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from analyze_results import JOB_RESULTS, cell
from bench import plan_shape


DEFAULT_ARCHIVE = JOB_RESULTS / "corrected" / "job-detailed.jsonl.gz"


@dataclass(frozen=True)
class Operator:
    path: str
    node: dict

    @property
    def label(self) -> str:
        name = self.node["Node Type"]
        target = self.node.get("Alias", self.node.get("Relation Name"))
        if target:
            name += f" on {target}"
        if index := self.node.get("Index Name"):
            name += f" using {index}"
        if subplan := self.node.get("Subplan Name"):
            name += f" ({subplan})"
        return name

    @property
    def qerror(self) -> float | None:
        if self.node.get("Actual Loops") != 1:
            return None
        estimated = max(1, self.node["Plan Rows"])
        observed = max(1, self.node["Actual Rows"])
        return max(estimated / observed, observed / estimated)


def operators(node: dict, path: str = "0"):
    """Traverse in plan order. Paths identify positions within each mode's plan."""
    yield Operator(path, node)
    for index, child in enumerate(node.get("Plans", [])):
        yield from operators(child, f"{path}.{index}")


def read_archive(path: Path) -> tuple[dict, dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    metadata = None
    records = {}
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected a JSON object")
                if metadata is None:
                    if "suite" not in row:
                        raise ValueError("expected benchmark metadata first")
                    metadata = row
                    continue
                query, arms = row.get("query"), row.get("arms")
                if not isinstance(query, str) or not query or not isinstance(arms, dict):
                    raise ValueError("expected a query name and mode records")
                if query in records:
                    raise ValueError(f"duplicate query {query!r}")
                for mode, measurement in arms.items():
                    if not isinstance(measurement, dict):
                        raise ValueError(f"expected a measurement object for {query}/{mode}")
                    if "plan" in measurement:
                        plan = measurement["plan"]
                        if not isinstance(plan, dict) or not isinstance(plan.get("Plan"), dict):
                            raise ValueError(f"expected an EXPLAIN plan for {query}/{mode}")
                records[query] = arms
            except ValueError as exc:
                raise ValueError(f"JSONL line {line_number}: {exc}") from exc
    if metadata is None or not records:
        raise ValueError("archive has no query records")
    return metadata, records


def number(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.4g}"


def worst_operator(rows: list[Operator]) -> str:
    scored = [(ratio, row) for row in rows if (ratio := row.qerror) is not None]
    if not scored:
        return "-"
    ratio, row = max(scored, key=lambda pair: pair[0])
    return cell(f"{row.label} ({number(ratio)}x)")


def append_overview(lines: list[str], records: dict, modes: list[str]) -> None:
    lines.extend([
        "## Plan overview",
        "",
        "The table lists the largest q-error among operators executed once in each "
        "saved plan. Use `--query` to see the full tree.",
        "",
        "| Query | Mode | Operators | Largest q-error |",
        "| --- | --- | ---: | --- |",
    ])
    for query, arms in records.items():
        for mode in modes:
            measurement = arms.get(mode, {})
            plan = measurement.get("plan")
            if plan is not None:
                rows = list(operators(plan["Plan"]))
                lines.append(f"| {cell(query)} | {cell(mode)} | {len(rows)} | {worst_operator(rows)} |")
            else:
                status = "No saved plan" if mode in arms else "Mode missing"
                lines.append(f"| {cell(query)} | {cell(mode)} | - | {status} |")


def append_query(lines: list[str], query: str, arms: dict, modes: list[str]) -> None:
    lines.extend([f"## {cell(query)}", ""])
    baseline = arms.get("baseline", {}).get("plan")
    learned = arms.get("learned", {}).get("plan")
    if baseline and learned and {"baseline", "learned"}.issubset(modes):
        changed = plan_shape(baseline["Plan"]) != plan_shape(learned["Plan"])
        lines.extend([f"Stock and learned plan shapes {'differ' if changed else 'match'}.", ""])

    for mode in modes:
        lines.extend([f"### {cell(mode)}", ""])
        measurement = arms.get(mode, {})
        plan = measurement.get("plan")
        if error := measurement.get("error"):
            lines.extend([f"Recorded failure {cell(error)}", ""])
        if plan is None:
            lines.extend(["No saved plan." if mode in arms else "Mode missing.", ""])
            continue

        root = plan["Plan"]
        items = [
            (plan, "Planning Time", "Planning time", " ms"),
            (plan, "Execution Time", "Execution time", " ms"),
            (root, "Shared Hit Blocks", "Shared blocks hit", ""),
            (root, "Shared Read Blocks", "Shared blocks read", ""),
            (root, "Temp Read Blocks", "Temporary blocks read", ""),
            (root, "Temp Written Blocks", "Temporary blocks written", ""),
            (measurement, "applied", "Feedback applications", ""),
        ]
        for source, key, label, unit in items:
            if key in source:
                value = f"{source[key]:,.3f}" if unit else number(source[key])
                lines.append(f"- {label} **{value}{unit}**")
        lines.extend([
            "",
            "| Path | Operator | Relationship | Estimated rows | Actual rows per loop | Loops | Q-error |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ])
        for row in operators(root):
            node = row.node
            fields = [
                row.path,
                cell(row.label),
                cell(node.get("Parent Relationship", "-")),
                number(node.get("Plan Rows")),
                number(node.get("Actual Rows")),
                number(node.get("Actual Loops")),
                number(row.qerror),
            ]
            lines.append("| " + " | ".join(fields) + " |")
        lines.append("")


def render(source: Path, metadata: dict, records: dict, queries: list[str],
           requested_modes: list[str]) -> str:
    available_modes = set().union(*(arms.keys() for arms in records.values()))
    # Preserve first appearance so the display follows the archive's benchmark order.
    modes = requested_modes or list(dict.fromkeys(mode for arms in records.values() for mode in arms))
    missing_modes = set(modes) - available_modes
    if missing_modes:
        raise ValueError(f"unknown mode: {', '.join(sorted(missing_modes))}")
    missing_queries = set(queries) - records.keys()
    if missing_queries:
        raise ValueError(f"unknown query: {', '.join(sorted(missing_queries))}")

    suite = cell(str(metadata["suite"]))
    lines = [
        "# Saved PostgreSQL query plans",
        "",
        f"Archive `{cell(source.name)}` contains {len(records)} queries from the {suite} suite.",
        "Q-error is the larger of estimate divided by actual rows and its inverse. "
        "Counts are floored at one for this calculation. Only operators with one "
        "execution receive a score. Paths describe positions within each individual plan.",
        "",
    ]
    if queries:
        for query in queries:
            append_query(lines, query, records[query], modes)
    else:
        append_overview(lines, records, modes)
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=DEFAULT_ARCHIVE,
                        help="benchmark JSONL or JSONL.GZ (default: corrected detailed run)")
    parser.add_argument("--query", action="append", default=[],
                        help="show the full tree for this query (repeatable)")
    parser.add_argument("--mode", action="append", default=[],
                        help="include this benchmark mode (repeatable)")
    parser.add_argument("-o", "--output", type=Path,
                        help="write Markdown to this path (default: standard output)")
    args = parser.parse_args()

    try:
        if args.output and args.output.resolve() == args.source.resolve():
            raise ValueError("report output must be a different file from the input archive")
        metadata, records = read_archive(args.source)
        report = render(args.source, metadata, records,
                        list(dict.fromkeys(args.query)), list(dict.fromkeys(args.mode)))
        if args.output:
            args.output.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
