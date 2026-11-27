"""Check that a benchmark CSV can be reproduced from its raw JSONL archive."""

import argparse
import csv
import io
import sys
from pathlib import Path
from typing import TextIO

from bench import summary
from inspect_plans import DEFAULT_ARCHIVE, read_archive


def index_csv(stream: TextIO) -> tuple[list[str], dict[tuple[str, str], dict[str, str]]]:
    reader = csv.DictReader(stream, strict=True)
    columns = reader.fieldnames or []
    if not {"query", "arm"}.issubset(columns) or len(set(columns)) != len(columns):
        raise ValueError("CSV requires unique column names including query and arm")

    rows = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"CSV line {reader.line_num} has the wrong number of fields")
        key = (row["query"], row["arm"])
        if not all(key):
            raise ValueError(f"CSV line {reader.line_num} needs a query and arm")
        if key in rows:
            raise ValueError(f"CSV line {reader.line_num} duplicates {key[0]}/{key[1]}")
        rows[key] = row
    return columns, rows


def compare_summary(records: dict, csv_path: Path) -> tuple[list[str], int]:
    rebuilt = io.StringIO()
    summary(({"query": query, "arms": arms} for query, arms in records.items()), rebuilt)
    rebuilt.seek(0)
    expected_columns, expected = index_csv(rebuilt)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        actual_columns, actual = index_csv(stream)

    issues = []
    missing_columns = set(expected_columns) - set(actual_columns)
    extra_columns = set(actual_columns) - set(expected_columns)
    if missing_columns:
        issues.append(f"Missing CSV columns: {', '.join(sorted(missing_columns))}")
    if extra_columns:
        issues.append(f"Unexpected CSV columns: {', '.join(sorted(extra_columns))}")

    for query, arm in sorted(expected.keys() - actual.keys()):
        issues.append(f"Missing CSV row for {query}/{arm}")
    for query, arm in sorted(actual.keys() - expected.keys()):
        issues.append(f"Unexpected CSV row for {query}/{arm}")

    shared_columns = [column for column in expected_columns if column in actual_columns]
    for key in sorted(expected.keys() & actual.keys()):
        for column in shared_columns:
            before, after = expected[key][column], actual[key][column]
            if before != after:
                issues.append(f"{key[0]}/{key[1]} {column}: archive {before!r}, CSV {after!r}")
    return issues, len(expected)


def hash_mismatches(records: dict) -> list[str]:
    mismatches = []
    for query, arms in records.items():
        hashes = {mode: row["result_hash"] for mode, row in arms.items() if row.get("result_hash")}
        if len(set(hashes.values())) > 1:
            details = ", ".join(f"{mode}={digest}" for mode, digest in hashes.items())
            mismatches.append(f"{query} has conflicting saved result hashes ({details})")
    return mismatches


def companion_csv(archive: Path) -> Path:
    if archive.suffix == ".gz":
        archive = archive.with_suffix("")
    return archive.with_suffix(".csv")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, nargs="?", default=DEFAULT_ARCHIVE,
                        help="benchmark JSONL or JSONL.GZ (default: corrected detailed run)")
    parser.add_argument("--csv", type=Path, help="summary CSV (defaults to the archive's companion CSV)")
    args = parser.parse_args()
    csv_path = args.csv or companion_csv(args.archive)

    try:
        _, records = read_archive(args.archive)
        issues, row_count = compare_summary(records, csv_path)
        issues.extend(hash_mismatches(records))
    except (OSError, EOFError, ValueError, KeyError, TypeError, csv.Error) as exc:
        print(f"Audit could not read {args.archive} and {csv_path}: {exc}", file=sys.stderr)
        return 2

    if issues:
        for issue in issues:
            print(issue, file=sys.stderr)
        print(f"Audit found {len(issues)} issue(s).", file=sys.stderr)
        return 1
    print(f"Audit passed for {len(records)} queries and {row_count} measurement rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
