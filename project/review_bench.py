"""Follow-up experiments for the IMDb evaluation."""
import concurrent.futures
import csv
import gzip
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import bench


TIMEOUT = 15000
RESET = "SELECT card_feedback_reset();"
ADVANCE = "CREATE TEMP TABLE advance_cutoff AS SELECT id FROM title WHERE id=1;"
OUT = Path(os.environ.get("CS631_OUTPUT", bench.CACHE / "measurements"))


def timeout_stages():
    """Use the saved records to locate each failure in the original driver."""
    path = bench.PROJECT.parent / "results" / "job" / "job-detailed.jsonl.gz"
    with gzip.open(path, "rt") as stream:
        next(stream)  # Run metadata.
        # Classify the first failed stage from the fields already saved.
        for row in map(json.loads, stream):
            timeouts = {}
            for mode, run in row["arms"].items():
                if "statement timeout" not in run.get("error", ""):
                    continue
                # An earlier failure leaves fewer fields in the run record.
                if "result_hash" not in run:
                    stage = "warm-up query"
                elif "plan" not in run:
                    stage = "EXPLAIN ANALYZE"
                elif "applied" not in run:
                    stage = "plan-only trace"
                else:
                    stage = f"timed run {len(run['ms']) + 1}"
                timeouts[mode] = stage
            yield row["query"], timeouts


def save_timeout_table():
    # Record where the timeout occurred so a missing measurement has a reason.
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "job-timeouts.csv").open("w", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("query", *bench.MODES))
        for query, timeouts in timeout_stages():
            writer.writerow((query, *(timeouts.get(mode, "") for mode in bench.MODES)))


def plan_only(db, mode, sql, *, settings=""):
    # Check which counts were applied without running the query.
    explained, hits = bench.explain(db, mode, "job", sql, TIMEOUT, debug=True,
                                    analyze=False, verbose=True, settings=settings)
    return explained["Plan"], hits


def time_query(db, mode, sql, *, settings=""):
    # Keep timeouts in the evidence and surface other errors normally.
    try:
        return {"ms": bench.elapsed(db, mode, "job", sql, TIMEOUT, settings=settings)}
    except subprocess.CalledProcessError as error:
        if "statement timeout" not in error.stderr:
            raise
        return {"timeout_ms": TIMEOUT}


def run_series(db, mode, sql, rounds, *, settings=""):
    # Start with an empty store and use separate calls to see when reuse begins.
    db.sql(RESET)
    runs = []
    for _ in range(rounds):
        plan, hits = plan_only(db, mode, sql, settings=settings)
        run = dict(time_query(db, mode, sql, settings=settings), hits=hits, plan=plan)
        runs.append(run)
        if "timeout_ms" in run:
            break
    return runs


def repeat_trials(db, queries):
    # Use the same query in both modes so the only difference is feedback.
    observations = {}
    for name, trials, rounds in (("13c", 3, 4), ("13d", 3, 4), ("16c", 1, 3), ("16d", 1, 3)):
        sql = queries[name]
        query_runs = []
        # Warm once before the trials using the ordinary estimator.
        reference = bench.result_hash(db, "disabled", "job", sql, TIMEOUT)
        for trial in range(trials):
            runs = {}
            modes = ("disabled", "learned") if trial % 2 == 0 else ("learned", "disabled")
            for mode in modes:
                runs[mode] = run_series(db, mode, sql, rounds)
            assert runs["learned"][0]["hits"] == 0
            assert all(r["hits"] == 0 for r in runs["disabled"])
            query_runs.append(runs)
        observations[name] = {"reference_result_hash": reference, "trials": query_runs}
        print(f"Finished repeat trials for JOB {name}", flush=True)
    return observations


def configure_metadata(db):
    # Record the settings that can affect plans and timings.
    return {key: db.sql(f"SHOW {key};").stdout.strip() for key in (
        "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
        "max_parallel_workers_per_gather", "jit", "autovacuum", "random_page_cost",
    )}


def save(name, value):
    # Keep detailed results in one compressed file per experiment.
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / f"{name}.json.gz", "wt") as output:
        json.dump(value, output, indent=2)


def repeat_experiment(db, queries):
    # Save new trials alongside the timeout stages from the archived runs.
    with db:
        results = repeat_trials(db, queries)
        save_timeout_table()
        save("job-review-repeats", {
            "result_hash_format": bench.RESULT_HASH_FORMAT,
            "job_revision": bench.JOB_REV, "job_data_sha256": bench.JOB_SHA256,
            "settings": configure_metadata(db), "statement_timeout_ms": TIMEOUT,
            "original_timeouts": dict(timeout_stages()), "queries": results,
        })


def timeout_diagnostics(db, queries):
    """Explain where the engine spent time if a separate longer run finishes."""
    # Use a longer limit to inspect queries that timed out in the original benchmark.
    limit = 45000
    records = {}
    with db:
        for name in ("16c", "16d"):
            sql = queries[name]
            case = records[name] = {}
            for mode in ("disabled", "learned"):
                db.sql(RESET)
                if mode == "learned":
                    # Match the original training order, with its two collector runs.
                    bench.result_hash(db, "collect", "job", sql, limit)
                    bench.explain(db, "collect", "job", sql, limit)
                trial = case[mode] = {}
                if mode == "learned" and name == "16d":
                    # The original 16d failure followed a successful learned SELECT.
                    trial["prior_learned_select"] = time_query(db, mode, sql)
                plan, hits = plan_only(db, mode, sql)
                trial.update(planned=plan, hits=hits)
                statement = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql};"
                try:
                    result = db.sql(
                        bench.transaction(mode, "job", statement, limit),
                        timeout=limit / 1000 + 15,
                    )
                    trial["observed"] = json.loads(result.stdout)[0]
                except subprocess.CalledProcessError as error:
                    if "statement timeout" not in error.stderr:
                        raise
                    trial["timeout_ms"] = limit
            print(f"Finished separate timeout diagnosis for JOB {name}", flush=True)
        save("job-review-timeouts", {"statement_timeout_ms": limit, "queries": records})


PROFILE = re.compile(r"card_feedback profile op=(\w+) ns=(\d+) acquire_ns=(\d+) hit=([01])")
SIZE = re.compile(r"card_feedback profile bytes=(\d+) entry=(\d+)")


def samples(stderr):
    # Read the total operation time separately from time spent acquiring the lock.
    return [{"operation": op, "ns": int(duration), "acquire_ns": int(acquire), "hit": int(hit)}
            for op, duration, acquire, hit in PROFILE.findall(stderr)]


def profile_sql(db, body):
    # DEBUG2 asks the profile build to record store operation times.
    settings = "SET LOCAL client_min_messages=DEBUG2;\n"
    sql = bench.transaction("learned", "job", settings + body, TIMEOUT)
    return db.sql(sql, timeout=120)


def pressure_test(db, sql):
    # Save a reference result before filling the store with other keys.
    db.sql(RESET)
    baseline = bench.result_hash(db, "collect", "job", sql, TIMEOUT)
    _, before = plan_only(db, "learned", sql)
    assert before > 0

    # Use distinct indexed probes on existing IMDb titles to fill the store.
    ids = db.sql("SELECT id FROM title ORDER BY id LIMIT 1100;").stdout.splitlines()
    assert len(ids) == 1100
    probes = "\n".join(f"SELECT id FROM title WHERE id={int(i)};" for i in ids)
    trace = profile_sql(db, "\\o /dev/null\n" + probes)
    size, entry = SIZE.search(trace.stderr).groups()
    _, after = plan_only(db, "learned", sql)
    assert after == 0
    assert bench.result_hash(db, "disabled", "job", sql, TIMEOUT) == baseline
    count = int(db.sql("SELECT count(*) FROM card_feedback_entries();").stdout)
    assert count == 1024
    return {"shared_reservation_bytes": int(size), "entry_bytes": int(entry),
            "capacity": count, "distinct_imdb_probes": len(ids),
            "hits_before": before, "hits_after": after, "samples": samples(trace.stderr)}


def client_trial(db, sql, gate):
    # Start readers together and mark the two runs so their hits can be counted separately.
    gate.wait(timeout=10)
    trace = profile_sql(db, f"SELECT pg_current_snapshot();\n"
                        f"\\warn REVIEW_FIRST\n{sql};\n\\warn REVIEW_SECOND\n{sql};")
    before, separator, second = trace.stderr.partition("REVIEW_SECOND")
    assert separator
    snapshot, first_row, second_row = trace.stdout.splitlines()
    assert first_row == second_row
    return {"snapshot": snapshot, "first_hits": before.count(bench.APPLIED_TRACE),
            "second_hits": second.count(bench.APPLIED_TRACE), "result": first_row,
            "samples": samples(trace.stderr)}


def readers_test(db, sql, readers):
    # Compare each reader's result to catch differences hidden by timing alone.
    gate = threading.Barrier(readers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=readers) as pool:
        pending = [pool.submit(client_trial, db, sql, gate) for _ in range(readers)]
        outcomes = [item.result() for item in pending]
    assert len({client["result"] for client in outcomes}) == 1
    return outcomes


def shared_store_trials(db, sql):
    # Measure a quiet server first, then repeat while a writer is active.
    idle = {}
    for readers in (1, 4):
        # Rebuild the same trained state before each reader count.
        db.sql(RESET)
        reference = bench.result_hash(db, "collect", "job", sql, TIMEOUT)
        for _ in range(4):
            assert bench.result_hash(db, "learned", "job", sql, TIMEOUT) == reference
        idle[str(readers)] = readers_test(db, sql, readers)
    assert all(client["first_hits"] > 0 and client["second_hits"] > 0
               for group in idle.values() for client in group)

    # An unrelated committed write can retire an otherwise reusable count.
    _, before = plan_only(db, "learned", sql)
    db.sql(ADVANCE)
    _, after = plan_only(db, "learned", sql)
    assert before > 0 and after == 0

    # Hold a private table's writer open during four readers of unchanged IMDb data.
    # A second connection advances the cutoff so the older writer is visible as active.
    with subprocess.Popen(db.psql_args(), text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as writer:
        try:
            writer.stdin.write("BEGIN; " + ADVANCE + " SELECT 'writer ready';\n")
            writer.stdin.flush()
            assert writer.stdout.readline().strip() == "writer ready"
            db.sql(ADVANCE)
            active = readers_test(db, sql, 4)
            for client in active:
                xmin, xmax, _ = client["snapshot"].split(":")
                assert xmin != xmax and client["first_hits"] == 0 and client["second_hits"] > 0
        finally:
            writer.communicate("ROLLBACK;\n", timeout=10)
    assert {c["result"] for c in active} == {c["result"] for c in idle["4"]}
    return {"quiescent": idle, "in_flight_writer": active,
            "unrelated_commit": {"before": before, "after": after}}


def use_profile_binary(db, run):
    # Only the diagnostic binary implements the forced local scope and lock timers.
    db.bin = run / "profile/bin"
    if not (db.bin / "postgres").exists():
        raise RuntimeError("Run bash project/run.sh build profile first.")


def profile_experiment(db, run, sql):
    # Combine contention and eviction measurements in the same record.
    use_profile_binary(db, run)
    with db:
        trials = shared_store_trials(db, sql)
        pressure = pressure_test(db, sql)
        assert pressure["samples"] and any(s["operation"] == "evict" for s in pressure["samples"])
        save("job-review-store", {
            "query": "13c", "job_revision": bench.JOB_REV, "job_data_sha256": bench.JOB_SHA256,
            "settings": configure_metadata(db), "readers": trials, "pressure": pressure,
        })
    print("Verified real IMDb feedback with readers, a writer, and a full store.", flush=True)


def scope_experiment(db, run, queries):
    """Change only the feedback key scope; every query gets a new connection."""
    # Use one binary and one dataset for both scope settings.
    use_profile_binary(db, run)
    settings = {
        "shared": "SET LOCAL card_feedback.local_scope = off;\n",
        "local": "SET LOCAL card_feedback.local_scope = on;\n",
    }
    cases = {}
    with db:
        for name in ("13c", "13d"):
            sql = queries[name]
            reference = bench.result_hash(db, "disabled", "job", sql, TIMEOUT)
            trials = []
            for trial in range(4):
                modes = ("local", "shared") if trial % 2 == 0 else ("shared", "local")
                results = {}
                for mode in modes:
                    runs = run_series(db, "learned", sql, 4, settings=settings[mode])
                    if "ms" in runs[-1]:
                        assert bench.result_hash(db, "learned", "job", sql, TIMEOUT,
                                                 settings=settings[mode]) == reference
                    results[mode] = runs
                assert all(r["hits"] == 0 for r in results["local"])
                assert results["shared"][0]["hits"] == 0 and results["shared"][1]["hits"] > 0
                trials.append(results)
            cases[name] = {"result_hash": reference, "trials": trials}
            print(f"Verified feedback scope control for JOB {name}", flush=True)

        # In the local arm, reuse still works inside the same transaction.
        db.sql(RESET)
        query = queries["13c"]
        body = query + f";\nEXPLAIN (FORMAT JSON) {query};"
        check = db.sql(bench.transaction("learned", "job", body, TIMEOUT, debug=True,
                                         settings=settings["local"]))
        within = check.stderr.count(bench.APPLIED_TRACE)
        _, across = bench.explain(db, "learned", "job", query, TIMEOUT, debug=True,
                                  analyze=False, settings=settings["local"])
        assert within > 0 and across == 0
        save("job-review-scope", {
            "job_revision": bench.JOB_REV, "job_data_sha256": bench.JOB_SHA256,
            "settings": configure_metadata(db), "statement_timeout_ms": TIMEOUT,
            "local_control_check": {"within_transaction": within, "new_connection": across},
            "queries": cases,
        })


def transfer_trials(db, sql, seeds, *, shared_scans=()):
    """Alternate two arms that differ only in whether they collect the seed counts."""
    # Warm the target with feedback off so it cannot seed itself.
    reference = bench.result_hash(db, "disabled", "job", sql, TIMEOUT)
    trials = []
    for trial in range(4):
        modes = ("control", "seeded") if trial % 2 == 0 else ("seeded", "control")
        arms = {}
        for mode in modes:
            db.sql(RESET)
            scan_ms = [
                bench.elapsed(db, "collect", "job", scan, TIMEOUT)
                for scan in shared_scans
            ]
            seed_mode = "collect" if mode == "seeded" else "disabled"
            seed_ms = [bench.elapsed(db, seed_mode, "job", seed, TIMEOUT) for seed in seeds]
            join_ms = bench.elapsed(db, "apply", "job", sql, TIMEOUT)
            trace = []
            plan, hits = bench.explain(db, "apply", "job", sql, TIMEOUT, debug=True,
                                       verbose=True, trace=trace)
            checksum = bench.result_hash(db, "apply", "job", sql, TIMEOUT)
            assert checksum == reference and hits == len(trace)
            arms[mode] = {"seed_ms": seed_ms, "join_ms": join_ms, "hits": hits,
                          "trace": trace, "result_hash": checksum, "plan": plan}
            # Include common scan time separately from the changed seed step.
            if shared_scans:
                arms[mode]["shared_scan_ms"] = scan_ms
        trials.append(arms)
    return trials


def transfer_experiment(db, queries):
    """Measure the first join execution with counts from standalone scans."""
    # Use literal filters from the target queries to seed standalone scan counts.
    title = "title <> '' AND (title LIKE 'Champion%' OR title LIKE 'Loser%')"
    rating = "info_type_id = 101 AND info > '5.0'"
    title_scan = f"SELECT count(*) FROM title WHERE {title}"
    rating_scan = f"SELECT count(*) FROM movie_info_idx WHERE {rating}"
    two_table = (
        "SELECT count(*) FROM title JOIN movie_info_idx ON title.id = movie_info_idx.movie_id "
        f"WHERE {title} AND {rating}"
    )
    cases = {
        "two-table": (two_table, (title_scan, rating_scan)),
        "3a": (queries["3a"], ("SELECT count(*) FROM title WHERE production_year > 2005",
                               "SELECT count(*) FROM keyword WHERE keyword LIKE '%sequel%'")),
        "6a": (queries["6a"], ("SELECT count(*) FROM keyword WHERE keyword = 'marvel-cinematic-universe'",
                               "SELECT count(*) FROM name WHERE name LIKE '%Downey%Robert%'")),
        "13c": (queries["13c"], (title_scan,
                                 "SELECT count(*) FROM company_name WHERE country_code = '[us]'")),
    }
    observations = {}
    with db:
        for name, (sql, seeds) in cases.items():
            seed_rows = [
                int(db.sql(bench.transaction("disabled", "job", seed + ";", TIMEOUT)).stdout)
                for seed in seeds
            ]
            trials = transfer_trials(db, sql, seeds)
            for arms in trials:
                assert arms["control"]["hits"] == 0
                assert all("scope=scan" in line for line in arms["seeded"]["trace"])
                if name == "two-table":
                    assert arms["seeded"]["hits"] == 2
            observations[name] = {"join_sql": sql, "seed_sql": seeds, "seed_rows": seed_rows, "trials": trials}
            print(f"Finished scan reuse trials for {name}", flush=True)

        # Train on different literals and check that the original join misses.
        db.sql(RESET)
        mismatched = (title_scan.replace("Champion%", "Winner%"),
                      rating_scan.replace("info_type_id = 101", "info_type_id = 100"))
        for seed in mismatched:
            bench.elapsed(db, "collect", "job", seed, TIMEOUT)
        _, hits = bench.explain(db, "apply", "job", two_table, TIMEOUT, debug=True, analyze=False)
        assert hits == 0
        save("job-scan-transfer", {
            "result_hash_format": bench.RESULT_HASH_FORMAT,
            "job_revision": bench.JOB_REV, "job_data_sha256": bench.JOB_SHA256,
            "patch_sha256": bench.digest_file(bench.PROJECT / "postgres.patch"),
            "settings": configure_metadata(db), "statement_timeout_ms": TIMEOUT,
            "mismatched_filters": {"seed_sql": mismatched, "hits": hits}, "queries": observations,
        })


def pair_experiment(db):
    """Hold the two saved scan counts constant while adding a saved pair count."""
    # Give both arms the same scan counts and vary only the collection of the pair count.
    cases = {
        "movie-ratings": (
            "title t", "t.title <> '' AND (t.title LIKE 'Champion%' OR t.title LIKE 'Loser%')",
            "movie_info_idx r", "r.info_type_id=101 AND r.info > '5.0'", "t.id=r.movie_id",
            "kind_type k ON t.kind_id=k.id", "k.kind='movie'",
        ),
        "cast-credits": (
            "name n", "n.name LIKE '%Downey%Robert%'",
            "cast_info c", "c.role_id=1", "n.id=c.person_id",
            "title t ON c.movie_id=t.id", "t.production_year > 2010",
        ),
    }
    observations = {}
    with db:
        for name, (left, left_filter, right, right_filter, on, third, third_filter) in cases.items():
            scans = (f"SELECT count(*) FROM {left} WHERE {left_filter}",
                     f"SELECT count(*) FROM {right} WHERE {right_filter}")
            from_pair = f"{left} JOIN {right} ON {on}"
            filters = f"{left_filter} AND {right_filter}"
            seed = f"SELECT count(*) FROM {from_pair} WHERE {filters}"
            sql = f"SELECT count(*) FROM ({from_pair}) JOIN {third} WHERE {filters} AND {third_filter}"
            seed_rows = int(db.sql(bench.transaction(
                "disabled",
                "job",
                seed + ";",
                TIMEOUT,
            )).stdout)
            trials = transfer_trials(db, sql, (seed,), shared_scans=scans)
            for arms in trials:
                control = arms["control"]["trace"]
                seeded = arms["seeded"]["trace"]
                assert len(control) == 2 and all("scope=scan" in line for line in control)
                assert [line for line in seeded if "scope=scan" in line] == control
                pairs = [line for line in seeded if "scope=pair" in line]
                assert len(seeded) == 3 and len(pairs) == 1 and f" learned={seed_rows} " in pairs[0]
            observations[name] = {"join_sql": sql, "scan_sql": scans, "seed_sql": seed,
                                  "seed_rows": seed_rows, "trials": trials}
            print(f"Finished two-table join reuse trials for {name}", flush=True)

        db.sql(RESET)
        case = observations["movie-ratings"]
        bench.elapsed(db, "collect", "job", case["seed_sql"], TIMEOUT)
        # A restriction on the third table can become an implicit restriction on the pair.
        extra_filter = case["join_sql"] + " AND k.id=1"  # Adds an implied restriction on t.kind_id.
        trace = []
        bench.explain(
            db,
            "apply",
            "job",
            extra_filter,
            TIMEOUT,
            debug=True,
            analyze=False,
            trace=trace,
        )
        assert not any("scope=pair" in line for line in trace), trace
        save("job-pair-transfer", {
            "result_hash_format": bench.RESULT_HASH_FORMAT,
            "job_revision": bench.JOB_REV, "job_data_sha256": bench.JOB_SHA256,
            "patch_sha256": bench.digest_file(bench.PROJECT / "postgres.patch"),
            "settings": configure_metadata(db), "statement_timeout_ms": TIMEOUT,
            "implied_filter": {"sql": extra_filter, "trace": trace}, "queries": observations,
        })


def main():
    # Run the selected experiment against the existing IMDb cluster.
    run = Path(sys.argv[1])
    phase = sys.argv[2]
    if phase not in ("all", "repeats", "profile", "timeouts", "scope", "transfer", "pair"):
        sys.exit("Use review [all|repeats|profile|timeouts|scope|transfer|pair].")
    _, patched = bench.prepare(run, "job")
    if phase in ("all", "pair"):
        pair_experiment(patched)
    if phase == "pair":
        return
    queries = bench.workload("job", "all")
    if phase in ("all", "transfer"):
        transfer_experiment(patched, queries)
    if phase in ("all", "repeats"):
        repeat_experiment(patched, queries)
    if phase in ("all", "profile"):
        profile_experiment(patched, run, queries["13c"])
    if phase in ("all", "timeouts"):
        timeout_diagnostics(patched, queries)
    if phase in ("all", "scope"):
        scope_experiment(patched, run, queries)


if __name__ == "__main__":
    main()
