"""Build reproducible datasets and benchmark four PostgreSQL modes."""
import csv
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from statistics import median

PROJECT = Path(__file__).resolve().parent
CACHE = PROJECT.parent / ".local"
JOB_REV = "a39603662e023e449cb2121997a5034df9e02ebf"
JOB_DATA = "https://event.cwi.nl/da/job/imdb.tgz"
JOB_SHA256 = "25f9d893c54f903366e0c263f88db0d429dbc2b159d4987ebc1e203242a7e988"
MODES = ("baseline", "disabled", "collect", "learned")
APPLIED_TRACE = "card_feedback applied signature="
RESULT_HASH_FORMAT = "postgres-copy-binary-v1"
DEMO = {
    "single": "SELECT * FROM correlated WHERE a=7",
    "join2": "SELECT c.id, d.label FROM correlated c JOIN lookup_one d ON d.id=c.id WHERE c.a=7 AND c.b=7",
    "join3": """SELECT c.id, d.label, e.label FROM correlated c
JOIN lookup_one d ON d.id=c.id JOIN lookup_two e ON e.id=c.id WHERE c.a=7 AND c.b=7""",
    "fanout": """SELECT f.id, d.label FROM fact f JOIN lookup_large d ON d.id=f.id
WHERE f.a=7 AND f.b=7 AND f.c=7 AND f.d=7 AND f.e=7""",
    "correlated": "SELECT * FROM correlated WHERE a=7 AND b=7",
}


def command(args, **options):
    # Accept Path objects so callers can build commands without shell quoting.
    return subprocess.run([str(arg) for arg in args], check=True, **options)


def digest_file(path):
    # Read the file in chunks so hashing the dataset does not load it into memory.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Cluster:
    """A local server that accepts connections only on a private Unix socket."""

    def __init__(self, run, suite, mode):
        # Keep each server's files and socket separate so the two builds can run together.
        self.bin = run / mode / "bin"
        self.data = run / f"{suite}-{mode}"
        self.socket = run / f"{suite}-{mode}-socket"
        self.socket.mkdir(mode=0o700, exist_ok=True)
        self.log = run / f"{suite}-{mode}.log"

    def ready(self):
        # The marker is written only after the complete dataset has loaded.
        return (self.data / ".cs631-ready").exists()

    def init(self):
        # Keep a failed load visible. A partial database would give misleading results.
        if self.data.exists():
            raise RuntimeError(f"Incomplete data load in {self.data}. Use a fresh run directory or move it aside.")

        # Use one locale for sorting and allow connections through the private socket only.
        command(
            [
                self.bin / "initdb",
                "-D", self.data,
                "-U", "cs631",
                "--locale=C",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            stdout=subprocess.DEVNULL,
        )

        # Keep memory settings fixed and turn off parallel work, JIT, and autovacuum.
        with (self.data / "postgresql.conf").open("a") as stream:
            stream.write("""
listen_addresses = ''
port = 55483
unix_socket_permissions = 0700
autovacuum = off
shared_buffers = '256MB'
work_mem = '32MB'
maintenance_work_mem = '512MB'
max_parallel_workers_per_gather = 0
max_parallel_maintenance_workers = 0
jit = off
""")

    def __enter__(self):
        # Wait for the server to accept connections before sending benchmark queries.
        command([self.bin / "pg_ctl", "-D", self.data, "-l", self.log,
                 "-o", f"-k {self.socket}", "-w", "start"], stdout=subprocess.DEVNULL)
        return self

    def __exit__(self, *error):
        # Stop the server even if a query failed, so the next run can start cleanly.
        command([self.bin / "pg_ctl", "-D", self.data, "-w", "-m", "fast", "stop"],
                stdout=subprocess.DEVNULL)

    def psql_args(self, *args):
        # Ignore local psql startup files and stop as soon as a SQL statement fails.
        return [self.bin / "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                "-h", self.socket, "-p", "55483", "-U", "cs631", "-d", "postgres", *args]

    def sql(self, sql, *args, timeout=45, raw=False):
        # Clear session defaults from the environment and capture results and plan notices.
        env = dict(os.environ, LC_ALL="C", PGOPTIONS="")
        try:
            return command(
                self.psql_args(*args),
                input=sql.encode("utf-8") if raw else sql,
                text=not raw,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # The benchmark includes SQL errors in its JSON records.
            if isinstance(error.stderr, bytes):
                error.stderr = error.stderr.decode("utf-8", errors="replace")
            raise

    def load_sql(self, sql):
        # Load interactively so dataset errors are visible immediately.
        command(self.psql_args(), input=sql, text=True)


def job_source():
    # Use one fixed query revision so reruns read the same SQL.
    path = CACHE / "workloads" / "job"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        command(["git", "clone", "--quiet", "https://github.com/gregrahn/join-order-benchmark.git", path])
    got = command(["git", "-C", path, "rev-parse", "HEAD"], text=True,
                  capture_output=True).stdout.strip()
    if got != JOB_REV:
        command(["git", "-C", path, "checkout", "--quiet", "--detach", JOB_REV])
    return path


def load_job(db):
    # Check the archive before loading it, and use JOB's indexes for both servers.
    source = job_source()
    data = CACHE / "bench" / "job"
    archive = data.with_name("imdb.tgz")
    data.mkdir(parents=True, exist_ok=True)
    # Skip unpacking once the archive has been checked and extracted.
    if not (data / ".extracted").exists():
        # Download to a temporary file so an interrupted download is not reused.
        if not archive.exists():
            partial = archive.with_suffix(".part")
            command(["curl", "--fail", "--location", "--retry", "3", "-o", partial, JOB_DATA])
            partial.rename(archive)
        if digest_file(archive) != JOB_SHA256:
            raise ValueError(f"Unexpected checksum for {archive}")
        command(["tar", "-xzf", archive, "-C", data])
        (data / ".extracted").touch()
    db.load_sql((source / "schema.sql").read_text())
    for table in sorted(data.glob("*.csv")):
        print(f"Loading JOB {table.stem}", flush=True)
        db.load_sql(f"\\copy {table.stem} FROM '{table}' WITH (FORMAT csv, ESCAPE E'\\\\');")
    db.load_sql((source / "fkindexes.sql").read_text() + "\nVACUUM ANALYZE;\n")


def prepare(run, suite):
    # Load data once and clone the stopped stock cluster so both builds start alike.
    base = Cluster(run, suite, "base")
    patched = Cluster(run, suite, "feedback")
    if not base.ready():
        base.init()
        with base:
            if suite == "job":
                load_job(base)
            else:
                base.load_sql((PROJECT / "demo.sql").read_text() + "\nVACUUM ANALYZE;")
        (base.data / ".cs631-ready").touch()
    # Give the patched build the same data and statistics as stock.
    if not patched.ready():
        if patched.data.exists():
            raise RuntimeError(f"Incomplete clone in {patched.data}")
        # Copy after clean shutdown so both arms have exactly the same data and statistics.
        shutil.copytree(base.data, patched.data, ignore=shutil.ignore_patterns(".cs631-ready"))
        with patched:
            patched.load_sql("CREATE EXTENSION IF NOT EXISTS card_feedback;")
        (patched.data / ".cs631-ready").touch()
    return base, patched


def workload(suite, scope):
    # Use a fixed short list for smoke runs and all numbered SQL files for the full run.
    if suite == "demo":
        return DEMO
    source = job_source()
    names = ["1a", "2a", "3a", "6a", "8a", "10a", "17a", "20a", "26a", "32a"]
    files = sorted(source.glob("[0-9]*.sql")) if scope == "all" else [source / f"{name}.sql" for name in names]
    return {file.stem: file.read_text().strip().rstrip(";") for file in files}


def transaction(mode, suite, sql, timeout=25000, debug=False, *, settings=""):
    # Use a fixed read-only snapshot. Each mode toggles collection and application separately.
    setup = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
    setup += f"SET LOCAL statement_timeout = {timeout};\n"
    # Keep the demo's cost settings fixed so its small examples are reproducible.
    if suite == "demo":
        setup += "SET LOCAL random_page_cost=8; SET LOCAL enable_mergejoin=off;\n"
    # Choose which switch to enable for the current comparison arm.
    if mode in ("collect", "learned"):
        setup += "SET LOCAL enable_card_feedback_collect=on;\n"
    if mode in ("apply", "learned"):
        setup += "SET LOCAL enable_card_feedback_apply=on;\n"
    if debug:
        setup += "SET LOCAL client_min_messages=DEBUG1;\n"
    return setup + settings + sql + "\nCOMMIT;\n"


def explain(db, mode, suite, sql, timeout, debug=False, *, analyze=True, verbose=False,
            settings="", trace=None):
    # Use JSON for plan checks. Turn on execution only when actual rows are needed.
    options = ["ANALYZE", "BUFFERS", "TIMING OFF", "SUMMARY ON"] if analyze else []
    if verbose:
        options.append("VERBOSE")
    options.append("FORMAT JSON")
    statement = f"EXPLAIN ({', '.join(options)}) {sql};"
    result = db.sql(
        transaction(mode, suite, statement, timeout, debug, settings=settings),
        timeout=timeout / 1000 + 15,
    )
    # Save only applied-count lines when a test requests the trace.
    if trace is not None:
        trace.extend(line for line in result.stderr.splitlines() if APPLIED_TRACE in line)
    return json.loads(result.stdout)[0], result.stderr.count(APPLIED_TRACE)


def binary_copy_rows(data):
    """Split PostgreSQL binary COPY into complete, length-delimited rows."""
    signature = b"PGCOPY\n\xff\r\n\0"
    if len(data) < 21 or not data.startswith(signature):
        raise ValueError("Invalid binary COPY header")
    flags, extension_length = struct.unpack_from("!II", data, len(signature))
    if flags:
        raise ValueError("Unexpected binary COPY flags")
    offset = len(signature) + 8 + extension_length

    while offset + 2 <= len(data):
        start = offset
        fields, = struct.unpack_from("!h", data, offset)
        offset += 2
        if fields == -1 and offset == len(data):
            return
        if fields < 0:
            break
        for _ in range(fields):
            if offset + 4 > len(data):
                raise ValueError("Truncated binary COPY field header")
            length, = struct.unpack_from("!i", data, offset)
            offset += 4
            # A length of -1 means NULL. A length of zero means an empty value.
            if length < -1 or offset + length > len(data):
                raise ValueError("Invalid binary COPY field length")
            offset += max(length, 0)
        yield data[start:offset]
    raise ValueError("Missing binary COPY trailer")


def result_hash(db, mode, suite, sql, timeout, *, settings=""):
    # COPY plans the SELECT directly and preserves NULLs, values, and duplicate rows.
    statement = f"COPY (\n{sql}\n) TO STDOUT (FORMAT binary);"
    result = db.sql(
        transaction(mode, suite, statement, timeout, settings=settings),
        timeout=timeout / 1000 + 15,
        raw=True,
    )
    fingerprint = hashlib.sha256()
    for row in sorted(binary_copy_rows(result.stdout)):
        fingerprint.update(row)
    return fingerprint.hexdigest()


def elapsed(db, mode, suite, sql, timeout, *, settings=""):
    # psql computes this figure through receipt of results. Connection, transaction
    # setup and CSV rendering are outside the interval.
    statement = f"\\o /dev/null\n\\timing on\n{sql};\n\\timing off\n\\o"
    output = db.sql(
        transaction(mode, suite, statement, timeout, settings=settings),
        timeout=timeout / 1000 + 15,
    )
    times = re.findall(r"^Time: ([\d.]+) ms", output.stdout, re.M)
    if len(times) != 1:
        raise RuntimeError(f"Expected one psql measurement, got {output.stdout[:200]!r}")
    return float(times[0])


def nodes(node):
    # Skip separate subplans when looking for the main scan or join.
    yield node
    for child in node.get("Plans", []):
        if child.get("Parent Relationship") not in ("InitPlan", "SubPlan"):
            yield from nodes(child)


def plan_shape(node):
    # Keep operator names and index choices, without the costs or timings.
    label = node["Node Type"]
    if "Relation Name" in node:
        label += ":" + node.get("Alias", node["Relation Name"])
    details = [node[key] for key in ("Join Type", "Strategy", "Index Name", "Scan Direction") if key in node]
    if details:
        label += "[" + "/".join(details) + "]"
    children = [plan_shape(child) for child in node.get("Plans", [])]
    return f"{label}({','.join(children)})" if children else label


def cardinality(plan):
    # Compare the top input relation under projection/aggregation/sort operators.
    # Explicitly omit observations where a loop never ran or ran repeatedly.
    operators = list(nodes(plan["Plan"]))
    candidates = [node for node in operators if node.get("Actual Loops") == 1]
    joins = [
        node
        for node in candidates
        if node["Node Type"] in ("Nested Loop", "Hash Join", "Merge Join")
    ]
    scans = [node for node in candidates if "Relation Name" in node]
    if joins or scans:
        node = (joins or scans)[0]
        estimate, actual = node["Plan Rows"], node["Actual Rows"]
        qerror = max(max(estimate, 1) / max(actual, 1), max(actual, 1) / max(estimate, 1))
        return estimate, actual, qerror
    return None, None, None


def one_query(base, patched, suite, name, sql, repetitions, timeout):
    # Start each query with an empty store so feedback from earlier queries cannot leak in.
    data = {"query": name, "sql": sql, "arms": {}}
    patched.sql("SELECT card_feedback_reset();")
    modes = MODES + (("statistics",) if suite == "demo" and name == "correlated" else ())
    errors = (subprocess.CalledProcessError, subprocess.TimeoutExpired)
    for mode in modes:
        db = base if mode in ("baseline", "statistics") else patched
        if mode == "statistics":
            base.sql("CREATE STATISTICS demo_dependencies (dependencies, mcv) ON a, b FROM correlated; "
                     "ANALYZE correlated;")
        arm = data["arms"][mode] = {"ms": []}
        try:
            # Warm by fetching the same result. Collection mode starts learning here.
            arm["result_hash"] = result_hash(db, mode, suite, sql, timeout)
            arm["plan"], _ = explain(db, mode, suite, sql, timeout)
            arm["applied"] = 0
            if mode == "learned":
                # Count hits with plain EXPLAIN. Keep DEBUG logging out of plan timings.
                _, arm["applied"] = explain(db, mode, suite, sql, timeout,
                                            debug=True, analyze=False)
        except errors as error:
            arm["error"] = (error.stderr or str(error)).strip()[:1000]
        finally:
            if mode == "statistics":
                base.sql("DROP STATISTICS demo_dependencies;")
    # Rotate timed arms after warming and training, avoiding a fixed run order.
    # Use a fixed shuffle seed so the run order can be reproduced.
    randomizer = random.Random(631)
    timed_modes = list(MODES)
    for _ in range(repetitions):
        randomizer.shuffle(timed_modes)
        for mode in timed_modes:
            arm = data["arms"][mode]
            if "error" in arm:
                continue
            db = base if mode == "baseline" else patched
            try:
                arm["ms"].append(elapsed(db, mode, suite, sql, timeout))
            except errors as error:
                arm["error"] = (error.stderr or str(error)).strip()[:1000]
    # Stop if completed arms returned different results.
    hashes = {arm["result_hash"] for arm in data["arms"].values() if "result_hash" in arm}
    if len(hashes) > 1:
        raise AssertionError(f"Result mismatch in {suite}/{name}")
    return data


def summary(results, output):
    # Write one CSV row per mode so runs can be compared by query.
    columns = ("query", "arm", "estimated", "actual", "qerror", "planning_ms", "execution_ms",
               "median_ms", "min_ms", "max_ms", "applied", "result_hash", "plan", "error")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    for row in results:
        for mode, arm in row["arms"].items():
            plan = arm.get("plan")
            samples = arm["ms"]
            writer.writerow((row["query"], mode, *(cardinality(plan) if plan else ("",) * 3),
                             plan.get("Planning Time") if plan else "",
                             plan.get("Execution Time") if plan else "",
                             median(samples) if samples else "", min(samples) if samples else "",
                             max(samples) if samples else "", arm.get("applied", ""),
                             arm.get("result_hash", ""), plan_shape(plan["Plan"]) if plan else "",
                             arm.get("error", "")))


def benchmark(run, suite, scope):
    # Keep the full settings and plans in the compressed log for later checks.
    base, patched = prepare(run, suite)
    queries = workload(suite, scope)
    if selection := os.environ.get("CS631_QUERIES"):
        queries = {name: queries[name] for name in selection.split(",")}
    repetitions = 7 if suite == "demo" else (3 if scope == "all" else 5)
    repetitions = int(os.environ.get("CS631_REPETITIONS", repetitions))
    timeout = int(os.environ.get("CS631_TIMEOUT_MS", "15000" if suite == "job" else "25000"))
    path = Path(os.environ.get("CS631_OUTPUT", CACHE / "measurements"))
    path.mkdir(parents=True, exist_ok=True)
    stem = f"{suite}-{scope}" if suite != "demo" else suite
    stem = os.environ.get("CS631_RUN_NAME", stem)
    results = []
    with base, patched, gzip.open(path / f"{stem}.jsonl.gz", "wt") as raw:
        version = base.sql("SELECT version();").stdout.strip()
        metadata = {"suite": suite, "scope": scope, "repetitions": repetitions, "timeout_ms": timeout,
                    "server": version, "host": os.uname().machine,
                    "settings": base.sql("SHOW ALL;").stdout, "result_hash_format": RESULT_HASH_FORMAT,
                    "job_revision": JOB_REV, "job_data_sha256": JOB_SHA256}
        raw.write(json.dumps(metadata) + "\n")
        for name, sql in queries.items():
            data = one_query(base, patched, suite, name, sql, repetitions, timeout)
            results.append(data)
            raw.write(json.dumps(data) + "\n")
            raw.flush()
            parts = []
            for mode, arm in data["arms"].items():
                if "error" in arm:
                    parts.append(f"{mode} ERROR")
                else:
                    qerror = cardinality(arm["plan"])[2]
                    ms = f"{median(arm['ms']):.2f} ms" if arm["ms"] else "untimed"
                    parts.append(f"{mode} {ms}, Q={qerror:.2f}" if qerror else f"{mode} {ms}")
            print(f"{name} | " + " | ".join(parts), flush=True)
    # Write a smaller summary next to the complete record.
    with (path / f"{stem}.csv").open("w", newline="") as output:
        summary(results, output)
    print(f"Saved {stem} plans and measurements in {path}", flush=True)


if __name__ == "__main__":
    run_dir = Path(sys.argv[1])
    suite = sys.argv[2]
    if suite not in ("demo", "job"):
        sys.exit("Use 'demo' or 'job'.")
    scope = sys.argv[3] if len(sys.argv) > 3 else "quick"
    if scope not in ("quick", "all"):
        sys.exit("Use 'quick' or 'all'.")
    benchmark(run_dir, suite, scope)
