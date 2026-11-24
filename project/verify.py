"""Check feedback correctness on the project fixture database."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

from bench import APPLIED_TRACE, DEMO, cardinality, nodes, prepare
from bench import explain as explain_query, result_hash as hash_query, transaction as query_transaction

QUERY = DEMO["correlated"]
RESET = "SELECT card_feedback_reset();"
TIMEOUT = 25000


def explain(db, sql=QUERY, *, mode="apply", analyze=False, settings="", trace=None):
    # Use plan-only checks by default so a probe cannot collect new counts.
    return explain_query(
        db, mode, "demo", sql, TIMEOUT, debug=True,
        analyze=analyze, settings=settings, trace=trace,
    )


def result_hash(db, sql=QUERY, *, mode="apply", settings=""):
    # Compare results separately from plan choice and trace output.
    return hash_query(db, mode, "demo", sql, TIMEOUT, settings=settings)


def transaction(mode, sql, *, settings="", debug=False):
    # Use the benchmark's session settings so tests exercise the same reuse rules.
    return query_transaction(mode, "demo", sql, TIMEOUT, settings=settings, debug=debug)


def feedback_entries(db):
    # Read the same scoped store contents that the SQL extension shows to users.
    text = db.sql("SELECT COALESCE(json_agg(e), '[]') FROM card_feedback_entries() AS e;").stdout
    return json.loads(text)


def shared_scan(db):
    # Run twice in separate connections to check cross-connection reuse.
    # The first query learns. The second should see the learned estimate.
    cold, hits = explain(db, QUERY, mode="learned", analyze=True)
    hot, hot_hits = explain(db, QUERY, mode="learned", analyze=True)
    assert cardinality(cold)[:2] == (10, 1000) and hits == 0
    assert cardinality(hot)[:2] == (1000, 1000) and hot_hits == 1
    assert result_hash(db, QUERY, mode="disabled") == result_hash(db, QUERY, mode="learned")


def in_flight_writer(db):
    # An older in-flight writer makes the snapshot private even on this idle server.
    with sql_session(db) as writer:
        read_until_marker(writer, "BEGIN; UPDATE correlated SET b=-1 WHERE id=7;")
        db.sql("CREATE TEMP TABLE advance_snapshot (id int);")
        statements = (f"SELECT pg_current_snapshot();\n"
                      f"EXPLAIN (ANALYZE, FORMAT JSON) {QUERY};\n"
                      f"EXPLAIN (ANALYZE, FORMAT JSON) {QUERY};")
        probe = db.sql(transaction("learned", statements, debug=True))
        xmin, xmax, _running = probe.stdout.splitlines()[0].split(":")
        assert xmin != xmax and probe.stderr.count(APPLIED_TRACE) == 1
        _, other_hits = explain(db, QUERY, mode="learned", analyze=True)
        assert other_hits == 0


def role_isolation(db):
    # Give the guest access to the same table so only the role changes.
    db.sql("CREATE ROLE cs631_guest;")
    try:
        db.sql("GRANT SELECT ON correlated TO cs631_guest;")
        explain(db, QUERY, mode="collect", analyze=True)
        guest = "SET ROLE cs631_guest;\n" + transaction(
            "learned", f"EXPLAIN (ANALYZE, FORMAT JSON) {QUERY};")
        guest_plan = json.loads(db.sql(guest).stdout)[0]
        assert cardinality(guest_plan)[:2] == (10, 1000)
    finally:
        db.sql("REVOKE SELECT ON correlated FROM cs631_guest; DROP ROLE cs631_guest;")


def update_and_analyze(db):
    # Train before changing the data so the later miss proves an old count was bypassed.
    explain(db, mode="collect", analyze=True)
    # Commit one row change and keep the fixture reversible for later tests.
    db.sql("UPDATE correlated SET b=-1 WHERE id=7;")
    try:
        changed, hits = explain(db, QUERY, mode="learned", analyze=True)
        assert cardinality(changed)[:2] == (10, 999) and hits == 0
        renewed, hits = explain(db, QUERY, mode="learned", analyze=True)
        assert cardinality(renewed)[:2] == (999, 999) and hits == 1
        db.sql("ANALYZE correlated;")
        _, hits = explain(db, QUERY, mode="learned", analyze=True)
        assert hits == 0
    finally:
        db.sql("UPDATE correlated SET b=7 WHERE id=7; VACUUM ANALYZE correlated;")


def truncated_table(db):
    # Start with a populated table so truncation changes both storage and row count.
    db.sql("CREATE TABLE rewrite_test AS SELECT id FROM correlated WHERE a=7;")
    try:
        scan = "SELECT id FROM rewrite_test"
        explain(db, scan, mode="learned", analyze=True)
        db.sql("TRUNCATE rewrite_test;")
        empty, hits = explain(db, scan, mode="learned", analyze=True)
        assert cardinality(empty)[1] == 0 and hits == 0
        empty, hits = explain(db, scan, mode="learned", analyze=True)
        assert cardinality(empty)[:2] == (1, 0) and hits == 1
    finally:
        db.sql("DROP TABLE rewrite_test;")


def repeated_joins(db):
    # Reset before each join size to keep the two-table seed out of the three-table case.
    for name in ("join2", "join3"):
        db.sql(RESET)
        first, _ = explain(db, DEMO[name], mode="learned", analyze=True)
        second, hits = explain(db, DEMO[name], mode="learned", analyze=True)
        assert cardinality(first)[:2] == (10, 1000)
        assert cardinality(second)[:2] == (1000, 1000) and hits >= 2
        if name == "join2":
            assert first["Plan"]["Node Type"] == "Nested Loop" and second["Plan"]["Node Type"] == "Hash Join"


def scan_transfer(db):
    # Seed both tables separately and change one filter or table at a time.
    seeds = (
        "SELECT count(*) FROM correlated AS x WHERE x.b=7 AND x.a=7",
        "SELECT count(*) FROM lookup_one WHERE label=7",
    )
    for seed in seeds:
        explain(db, seed, mode="collect", analyze=True)
    join = DEMO["join2"] + " AND d.label=7"
    # Count scan hits on the first plan of the previously unseen join.
    trace = []
    _, hits = explain(db, join, mode="apply", trace=trace)
    assert hits == 2 and all("scope=scan" in line for line in trace)
    assert any("raw=10 learned=1000" in line for line in trace)
    assert result_hash(db, join, mode="apply") == result_hash(db, join, mode="disabled")
    for other, expected in (
        (join.replace("d.label=7", "d.label=8"), 1),
        (join.replace("lookup_one", "lookup_two"), 1),
        (join + " AND c.id=7", 0),  # The join also implies d.id=7.
    ):
        _, hits = explain(db, other, mode="apply")
        assert hits == expected, (other, hits)


def pair_transfer(db):
    # Use an untrained larger join first as the control for the two-table seed.
    larger_join = DEMO["join3"]
    _, hits = explain(db, larger_join, mode="apply")
    assert hits == 0
    explain(db, DEMO["join2"], mode="collect", analyze=True)
    # Put the tables and two filter terms in a different order.
    reordered = ("SELECT c.id, d.label, e.label FROM lookup_two e "
                 "JOIN correlated c ON c.id=e.id JOIN lookup_one d ON c.id=d.id "
                 "WHERE c.b=7 AND c.a=7")
    for candidate in (larger_join, reordered):
        trace = []
        explain(db, candidate, mode="apply", trace=trace)
        pairs = [line for line in trace if "scope=pair" in line]
        assert len(pairs) == 1 and "learned=1000" in pairs[0], trace
    assert result_hash(db, larger_join, mode="apply") == result_hash(
        db, larger_join, mode="disabled")
    for candidate in (larger_join + " AND e.id=7", larger_join.replace("d.id=c.id", "d.id=c.a")):
        trace = []
        explain(db, candidate, mode="apply", trace=trace)
        assert not any("scope=pair" in line for line in trace), trace


def partial_and_prepared(db):
    # Try early stops and cached plans before checking the store for reusable counts.
    limited = QUERY + " LIMIT 5"
    explain(db, limited, mode="learned", analyze=True)
    db.sql(transaction("learned", f"DECLARE c CURSOR FOR {QUERY}; FETCH 5 FROM c; CLOSE c;"))
    assert not feedback_entries(db)
    db.sql(transaction("collect", QUERY + ";"))
    before = sum(entry["observations"] for entry in feedback_entries(db))
    cached = f"PREPARE p AS {QUERY}; EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE p;"
    prepared = json.loads(db.sql(transaction("learned", cached)).stdout)[0]
    assert cardinality(prepared)[:2] == (10, 1000)
    assert sum(entry["observations"] for entry in feedback_entries(db)) == before + 1


def partial_join_inputs(db):
    # Exercise two ways a join can finish without reading one child completely.
    inner_unique = ("SELECT c.id, d.label FROM correlated c JOIN lookup_one d ON d.id=c.id "
                    "WHERE (c.id=7 OR c.id=-1) AND c.a=7")
    settings = ("SET LOCAL enable_hashjoin=off; SET LOCAL enable_indexscan=off; "
                "SET LOCAL enable_indexonlyscan=off; SET LOCAL enable_bitmapscan=off; "
                "SET LOCAL enable_material=off;")
    db.sql(transaction("collect", settings + "\n" + inner_unique + ";"))
    assert sorted(entry["last_actual_rows"] for entry in feedback_entries(db)) == [1, 1]
    db.sql(RESET)
    # An empty hash build can leave the probe input partly read.
    empty_hash = DEMO["join2"] + " AND d.label=-1"
    db.sql(transaction("collect", "SET LOCAL enable_nestloop=off;\n" + empty_hash + ";"))
    assert sorted(entry["last_actual_rows"] for entry in feedback_entries(db)) == [0, 0]


def memoized_input(db):
    # Keep a nested loop so the plan can use Memoize on repeated inner lookups.
    memoized = "SELECT f.id, d.label FROM fact f JOIN lookup_large d ON d.id=f.a WHERE f.b=7"
    settings = "SET LOCAL enable_hashjoin=off; SET LOCAL enable_mergejoin=off;"
    sql = f"{settings} EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) {memoized};"
    memo_plan = json.loads(db.sql(transaction("collect", sql)).stdout)[0]
    assert any(node["Node Type"] == "Memoize" for node in nodes(memo_plan["Plan"]))
    assert sorted(entry["last_actual_rows"] for entry in feedback_entries(db)) == [50000, 50000]


def concurrent_counts(db):
    # Have several sessions update one key to check for lost increments.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: db.sql(transaction("collect", (QUERY + ";\n") * 5)), range(4)))
    entries = feedback_entries(db)
    assert len(entries) == 1 and entries[0]["observations"] == 20


def eviction(db):
    # Insert one more distinct key than the store can hold.
    statements = "\\o /dev/null\n" + "\n".join(f"SELECT id FROM correlated WHERE id={i};" for i in range(
        1,
        1026,
    ))
    db.sql(transaction("collect", statements))
    assert len(feedback_entries(db)) == 1024
    _, old_hits = explain(
        db,
        "SELECT id FROM correlated WHERE id=1",
        mode="learned",
        analyze=True,
    )
    _, new_hits = explain(
        db,
        "SELECT id FROM correlated WHERE id=1025",
        mode="learned",
        analyze=True,
    )
    assert old_hits == 0 and new_hits == 1


def switches(db):
    # Check stock behavior, recording, and application independently before combining them.
    estimate, _ = explain(db, analyze=True, mode="disabled")
    assert cardinality(estimate)[:2] == (10, 1000)
    assert not feedback_entries(db)

    _, hits = explain(db, mode="collect")
    assert hits == 0 and not feedback_entries(db)
    explain(db, analyze=True)
    assert not feedback_entries(db)

    _, hits = explain(db, analyze=True, mode="collect")
    assert hits == 0
    entry, = feedback_entries(db)
    assert entry["raw_est_rows"] == 10
    assert entry["last_actual_rows"] == entry["ema_actual_rows"] == 1000
    assert entry["observations"] == 1

    applied, hits = explain(db, analyze=True)
    assert hits == 1 and cardinality(applied)[:2] == (1000, 1000)
    assert feedback_entries(db) == [entry]

    _, hits = explain(db, analyze=True, mode="learned")
    updated, = feedback_entries(db)
    assert hits == 1 and updated["observations"] == 2
    assert updated["signature"] == entry["signature"]
    assert updated["last_updated"] >= entry["last_updated"]


def transaction_modes(db):
    # Start from a known hit and change only the transaction mode for each probe.
    explain(db, mode="collect", analyze=True)
    _, positive = explain(db)
    assert positive == 1
    # Apply-only probes should not alter the original observation.
    baseline = feedback_entries(db)

    begins = (
        "",
        "BEGIN READ ONLY;",
        "BEGIN ISOLATION LEVEL REPEATABLE READ READ WRITE;",
        "BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY;",
    )
    for begin in begins:
        sql = (
            f"{begin}\n"
            "SET enable_card_feedback_apply = on;\n"
            "SET client_min_messages = debug1;\n"
            f"EXPLAIN (ANALYZE, FORMAT JSON) {QUERY};\n"
        )
        if begin:
            sql += "ROLLBACK;\n"
        result = db.sql(sql)
        plan, = json.loads(result.stdout)
        assert cardinality(plan)[:2] == (10, 1000), begin
        assert APPLIED_TRACE not in result.stderr, begin
    assert feedback_entries(db) == baseline


def parallel_settings(db):
    # Setting a positive worker limit blocks reuse even if PostgreSQL picks a serial plan.
    explain(db, mode="collect", analyze=True)
    settings = "SET LOCAL max_parallel_workers_per_gather=2;"
    plan, hits = explain(db, analyze=True, settings=settings)
    assert hits == 0 and cardinality(plan)[:2] == (10, 1000)
    _, hits = explain(db)
    assert hits == 1


def access_paths(db):
    # Train and probe with different access methods for the same table and filter.
    query = "SELECT id FROM cs631_verify.sample WHERE id BETWEEN 5 AND 12"
    paths = (
        ("Seq Scan", """SET LOCAL enable_indexscan=off;
SET LOCAL enable_indexonlyscan=off;
SET LOCAL enable_bitmapscan=off;"""),
        ("Index Scan", """SET LOCAL enable_seqscan=off;
SET LOCAL enable_indexonlyscan=off;
SET LOCAL enable_bitmapscan=off;"""),
        ("Index Only Scan", """SET LOCAL enable_seqscan=off;
SET LOCAL enable_bitmapscan=off;"""),
        ("Bitmap Heap Scan", """SET LOCAL enable_seqscan=off;
SET LOCAL enable_indexscan=off;
SET LOCAL enable_indexonlyscan=off;"""),
    )
    # Save the result before forcing any of the access methods.
    reference = result_hash(db, query, mode="disabled")
    for index, (node, settings) in enumerate(paths):
        db.sql(RESET)
        observed, _ = explain(db, query, mode="collect", analyze=True, settings=settings)
        assert observed["Plan"]["Node Type"] == node, observed["Plan"]
        next_node, next_settings = paths[(index + 1) % len(paths)]
        applied, hits = explain(db, query, analyze=True, settings=next_settings)
        assert hits == 1 and cardinality(applied)[:2] == (8, 8)
        assert applied["Plan"]["Node Type"] == next_node
        assert result_hash(db, query, settings=next_settings) == reference


def count_below_aggregate(db):
    # Use an aggregate with fewer output rows than its input scan.
    seed = """SELECT label, count(*) FROM cs631_verify.sample
WHERE a=7 GROUP BY label ORDER BY label"""
    query = "SELECT id FROM cs631_verify.sample WHERE a=7"
    plan, _ = explain(db, seed, mode="collect", analyze=True)
    aggregate = next(node for node in nodes(plan["Plan"]) if node["Node Type"] == "Aggregate")
    correct = int(db.sql("SELECT count(*) FROM cs631_verify.sample WHERE a=7;").stdout)
    assert 0 < aggregate["Actual Rows"] < correct
    entry, = feedback_entries(db)
    assert entry["last_actual_rows"] == correct
    applied, hits = explain(db, query, analyze=True)
    assert hits == 1 and cardinality(applied)[:2] == (correct, correct)


def supported_filters(db):
    # Cover the expression forms used in transferable table keys.
    filters = (
        "a = ANY (ARRAY[2,3,5])",
        "label IS NULL",
        "flag IS TRUE",
        "flag IS NOT UNKNOWN AND NOT flag",
        "abs(a - 5) = 3",
        "label LIKE 'group-2%'",
        "label IS NOT NULL AND (a=7 OR a=8)",
        "(a + 1)::bigint = 6::bigint",
    )
    for condition in filters:
        db.sql(RESET)
        seed = f"SELECT count(*) FROM cs631_verify.sample WHERE {condition}"
        target = f"SELECT id FROM cs631_verify.sample WHERE {condition}"
        expected = int(db.sql(seed + ";").stdout)
        assert expected > 0, condition
        explain(db, seed, mode="collect", analyze=True)
        plan, hits = explain(db, target, analyze=True)
        assert hits == 1 and cardinality(plan)[:2] == (expected, expected), condition


def changed_predicate(db):
    # Hold the table fixed so a missed lookup can be traced to the changed filter.
    seed = "SELECT id FROM lookup_one WHERE label=7"
    explain(db, seed, mode="collect", analyze=True)
    _, hits = explain(db, seed)
    assert hits == 1

    variants = (
        ("SELECT id FROM lookup_one WHERE id=7", 1),
        ("SELECT id FROM lookup_one WHERE label<7", 70000),
        ("SELECT id FROM lookup_one WHERE label=7 OR label=8", 20000),
    )
    for query, expected in variants:
        plan, hits = explain(db, query, analyze=True)
        assert hits == 0 and cardinality(plan)[1] == expected, query


def excluded_query_forms(db):
    # Keep a matching base count in the store when trying each unsupported query form.
    explain(db, mode="collect", analyze=True)
    _, hits = explain(db)
    assert hits == 1
    cases = (
        (QUERY + " OFFSET 1", 999),
        (f"WITH q AS MATERIALIZED ({QUERY}) SELECT * FROM q", 1000),
        (f"SELECT * FROM ({QUERY} OFFSET 0) AS q", 1000),
        (QUERY + " AND id IN (SELECT id FROM lookup_one)", 1000),
        (f"SELECT row_number() OVER (ORDER BY id) FROM ({QUERY}) AS q", 1000),
        (f"SELECT id FROM ({QUERY}) AS q UNION ALL SELECT 0", 1001),
        ("SELECT a, count(*) FROM correlated WHERE a=7 AND b=7 GROUP BY ROLLUP(a)", 2),
        ("SELECT min(id) FROM correlated WHERE a=7 AND b=7 HAVING count(*) > 0", 1),
        ("SELECT c.id, d.label FROM correlated c LEFT JOIN lookup_one d ON c.id=d.id AND d.label=9 "
         "WHERE c.a=7 AND c.b=7", 1000),
    )
    for query, returned in cases:
        plan, hits = explain(db, query, analyze=True)
        assert hits == 0, query
        assert plan["Plan"]["Actual Rows"] == returned, query


def excluded_relation_kinds(db):
    # These have the same results as the permanent heap control.
    control = "SELECT id FROM cs631_verify.sample WHERE id < 9"
    explain(db, control, mode="collect", analyze=True)
    _, hits = explain(db, control)
    assert hits == 1
    queries = (
        "SELECT id FROM cs631_verify.unlogged_sample WHERE id < 9",
        "SELECT id FROM cs631_verify.partitioned_sample WHERE id < 9",
        "SELECT id FROM cs631_verify.inherited_sample WHERE id < 9",
        "SELECT id FROM cs631_verify.sample TABLESAMPLE SYSTEM (100) WHERE id < 9",
    )
    for query in queries:
        seeded, hits = explain(db, query, mode="learned", analyze=True)
        assert hits == 0 and seeded["Plan"]["Actual Rows"] == 8, query
        _, hits = explain(db, query)
        assert hits == 0, query

    body = """CREATE TEMP TABLE sample_temp AS
SELECT id FROM cs631_verify.sample WHERE id < 9;
"""
    scan = "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM sample_temp;"
    result = db.sql(body + transaction("learned", scan, debug=True))
    assert APPLIED_TRACE not in result.stderr
    plan, = json.loads(result.stdout)
    assert plan["Plan"]["Actual Rows"] == 8


def excluded_functions(db):
    # Stable functions and user-defined functions are outside the reusable expression set.
    for condition in (
        "cs631_verify.is_seven(a)",
        "a = current_setting('cs631_verify.cutoff')::int",
    ):
        query = f"SELECT id FROM correlated WHERE {condition} AND b=7"
        settings = "SET LOCAL cs631_verify.cutoff='7';"
        learned, hits = explain(db, query, mode="learned", analyze=True, settings=settings)
        assert hits == 0 and cardinality(learned)[1] == 1000, condition
        cold, hits = explain(db, query, analyze=True, settings=settings)
        assert hits == 0 and cardinality(cold)[1] == 1000, condition


def large_key_fallback(db):
    # Use a literal larger than the full key buffer to exercise the size check.
    suffix = "x" * 16384
    select = "SELECT id FROM cs631_verify.sample WHERE label="
    oversized = select + f"'{suffix}'"
    normal = select + "'group-7'"
    plan, _ = explain(db, oversized, mode="collect", analyze=True)
    assert cardinality(plan)[1] == 0
    _, hits = explain(db, oversized)
    assert hits == 0

    explain(db, normal, mode="collect", analyze=True)
    regular, hits = explain(db, normal, analyze=True)
    estimate, actual, _ = cardinality(regular)
    assert hits == 1 and 0 < actual == estimate


def same_name_in_other_schema(db):
    # Resolve one SQL name to different table IDs by changing the search path.
    query = "SELECT id FROM identical_name WHERE id > 0"
    first = "SET LOCAL search_path=cs631_verify;"
    second = "SET LOCAL search_path=cs631_verify_other;"
    explain(db, query, mode="collect", analyze=True, settings=first)
    a, hits = explain(db, query, analyze=True, settings=first)
    assert hits == 1 and cardinality(a)[:2] == (13, 13)

    # This is a different relation even though the query text is identical.
    b, hits = explain(db, query, analyze=True, settings=second)
    assert hits == 0 and cardinality(b)[1] == 5
    explain(db, query, mode="collect", analyze=True, settings=second)
    _, second_hits = explain(db, query, settings=second)
    _, first_hits = explain(db, query, settings=first)
    assert first_hits == second_hits == 1
    assert len(feedback_entries(db)) == 2


@contextmanager
def reading_role(db, relation):
    # Use a temporary test role with only enough access for the checks.
    db.sql("CREATE ROLE cs631_verify_reader;")
    try:
        db.sql(f"""GRANT USAGE ON SCHEMA cs631_verify TO cs631_verify_reader;
GRANT SELECT ON {relation} TO cs631_verify_reader;""")
        yield "SET ROLE cs631_verify_reader;\n"
    finally:
        db.sql("DROP OWNED BY cs631_verify_reader; DROP ROLE cs631_verify_reader;")


def row_security(db):
    # Use the same role with two policy values so visible rows can differ.
    query = "SELECT id FROM cs631_verify.secure ORDER BY id"
    with reading_role(db, "cs631_verify.secure") as role:
        for limit in (5, 19):
            settings = f"SET LOCAL cs631_verify.cutoff='{limit}';"
            sql = f"EXPLAIN (ANALYZE, FORMAT JSON) {query};"
            explain_result = db.sql(role + transaction(
                "learned",
                sql,
                settings=settings,
                debug=True,
            ))
            plan, = json.loads(explain_result.stdout)
            assert plan["Plan"]["Actual Rows"] == limit
            assert APPLIED_TRACE not in explain_result.stderr

            rows = db.sql(role + transaction("learned", query + ";", settings=settings))
            assert [int(row) for row in rows.stdout.splitlines()] == list(range(1, limit + 1))
            entries = db.sql(role + "SELECT count(*) FROM card_feedback_entries();")
            assert entries.stdout.strip() == "0"


def scoped_reset(db):
    # Seed two roles before resetting just the second role's entries.
    query = "SELECT id FROM cs631_verify.sample WHERE id < 9"
    with reading_role(db, "cs631_verify.sample") as role:
        explain(db, query, mode="collect", analyze=True)
        own_entry, = feedback_entries(db)
        seed = transaction("collect", "EXPLAIN (ANALYZE, FORMAT JSON) " + query + ";")
        db.sql(role + seed)
        role_size = db.sql(role + "SELECT count(*) FROM card_feedback_entries();")
        assert role_size.stdout.strip() == "1"

        # The reset must leave the original role's count available.
        removed = db.sql(role + RESET)
        assert removed.stdout.strip() == "1"
        assert db.sql(role + RESET).stdout.strip() == "0"
        assert feedback_entries(db) == [own_entry]
        _, hits = explain(db, query)
        assert hits == 1


def database_scope(db):
    # Create a second database so the SQL view and reset can be checked across databases.
    db.sql("CREATE DATABASE cs631_verify_isolation;")
    other = ("-d", "cs631_verify_isolation")
    try:
        db.sql("""
CREATE EXTENSION card_feedback;
CREATE TABLE isolated_db_sample (id int);
INSERT INTO isolated_db_sample VALUES (1), (2);
""", *other)
        explain(db, mode="collect", analyze=True)
        entries = feedback_entries(db)
        assert len(entries) == 1
        assert db.sql(
            "SELECT count(*) FROM card_feedback_entries();",
            *other,
        ).stdout.strip() == "0"

        simple = "SELECT id FROM isolated_db_sample WHERE id > 0"
        seed = transaction("collect", "EXPLAIN (ANALYZE, FORMAT JSON) " + simple + ";")
        db.sql(seed, *other)
        assert db.sql(
            "SELECT count(*) FROM card_feedback_entries();",
            *other,
        ).stdout.strip() == "1"
        assert db.sql(RESET, *other).stdout.strip() == "1"
        assert feedback_entries(db) == entries
    finally:
        db.sql("DROP DATABASE cs631_verify_isolation;")


def insert_and_delete(db):
    # Use committed changes and fresh sessions to test both directions of a count change.
    query = "SELECT id FROM cs631_verify.mutations WHERE id > 0"
    explain(db, query, mode="collect", analyze=True)
    try:
        db.sql("INSERT INTO cs631_verify.mutations VALUES (4), (5);")
        added, hits = explain(db, query, mode="learned", analyze=True)
        assert hits == 0 and cardinality(added)[1] == 5
        new, hits = explain(db, query, analyze=True)
        assert hits == 1 and cardinality(new)[:2] == (5, 5)

        db.sql("DELETE FROM cs631_verify.mutations WHERE id > 3;")
        removed, hits = explain(db, query, mode="learned", analyze=True)
        assert hits == 0 and cardinality(removed)[1] == 3
        new, hits = explain(db, query, analyze=True)
        assert hits == 1 and cardinality(new)[:2] == (3, 3)
    finally:
        db.sql("DELETE FROM cs631_verify.mutations WHERE id > 3;")


@contextmanager
def sql_session(db):
    # Keep one psql process alive across statements to hold its transaction open.
    with subprocess.Popen(db.psql_args(), text=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as client:
        try:
            client.stdin.write("SET statement_timeout='5s';\n")
            client.stdin.flush()
            yield client
        finally:
            client.communicate("ROLLBACK;\n", timeout=10)


def read_until_marker(client, sql):
    # The marker confirms PostgreSQL finished the preceding statements.
    mark = "cs631_read_complete"
    client.stdin.write(sql + f"\nSELECT '{mark}';\n")
    client.stdin.flush()
    lines = []
    for line in client.stdout:
        if line.strip() == mark:
            return "".join(lines)
        lines.append(line)
    raise AssertionError("The reader exited before finishing its SQL.")


def fixed_snapshot(db):
    # Leave the old reader open across a commit so we can check both views of the data.
    query = "SELECT id FROM cs631_verify.mutations WHERE id > 0"
    explain(db, query, mode="collect", analyze=True)
    with sql_session(db) as client:
        start = """BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL enable_card_feedback_apply=on;
SELECT count(*) FROM cs631_verify.mutations;"""
        assert read_until_marker(client, start).strip() == "3"
        db.sql("INSERT INTO cs631_verify.mutations VALUES (4), (5);")
        try:
            sql = f"EXPLAIN (ANALYZE, FORMAT JSON) {query};"
            old, = json.loads(read_until_marker(client, sql))
            assert cardinality(old)[:2] == (3, 3)

            # A new snapshot should miss and learn the committed rows.
            new, hits = explain(db, query, mode="learned", analyze=True)
            assert hits == 0 and cardinality(new)[1] == 5
            still_old, = json.loads(read_until_marker(client, sql))
            assert cardinality(still_old)[:2] == (3, 3)
        finally:
            db.sql("DELETE FROM cs631_verify.mutations WHERE id > 3;")


def relation_replacement(db):
    # Recreate the same name with a new table ID and a different count.
    query = "SELECT id FROM cs631_verify.replacement WHERE id > 0"
    explain(db, query, mode="collect", analyze=True)
    old_oid = db.sql("SELECT 'cs631_verify.replacement'::regclass::oid;").stdout
    db.sql("""DROP TABLE cs631_verify.replacement;
CREATE TABLE cs631_verify.replacement (id int);
INSERT INTO cs631_verify.replacement VALUES (1), (2), (3), (4);""")
    new_oid = db.sql("SELECT 'cs631_verify.replacement'::regclass::oid;").stdout
    assert old_oid != new_oid
    cold, hits = explain(db, query, mode="learned", analyze=True)
    assert hits == 0 and cardinality(cold)[1] == 4
    hot, hits = explain(db, query, analyze=True)
    assert hits == 1 and cardinality(hot)[:2] == (4, 4)


def bound_parameters(db):
    # Check both prepared plan modes with the same value as an existing scan count.
    explain(db, mode="collect", analyze=True)
    query = """PREPARE bound(int) AS SELECT id FROM correlated WHERE a=$1 AND b=$1;
EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE bound(7);"""
    for cache_mode in ("force_generic_plan", "force_custom_plan"):
        settings = f"SET LOCAL plan_cache_mode='{cache_mode}';"
        response = db.sql(transaction("apply", query, settings=settings, debug=True))
        plan, = json.loads(response.stdout)
        assert cardinality(plan)[1] == 1000
        assert APPLIED_TRACE not in response.stderr
    _, hits = explain(db)
    assert hits == 1


def exhausted_portal(db):
    # Reach the end with multiple cursor calls to check that EOF alone is insufficient.
    sql = f"""DECLARE c CURSOR FOR {QUERY};
MOVE FORWARD 5 IN c;
MOVE ALL IN c;
CLOSE c;"""
    db.sql(transaction("collect", sql))
    assert not feedback_entries(db)

    # A regular pass over the same rows can still teach the planner.
    explain(db, mode="collect", analyze=True)
    _, hits = explain(db)
    assert hits == 1


def merge_join_inputs(db):
    # Force a merge join so the test checks its child traversal rule.
    query = "SELECT c.id FROM correlated c JOIN lookup_one d ON c.id=d.id WHERE c.id < 100"
    settings = """SET LOCAL enable_nestloop=off;
SET LOCAL enable_hashjoin=off;
SET LOCAL enable_mergejoin=on;"""
    plan, _ = explain(db, query, mode="collect", analyze=True, settings=settings)
    assert plan["Plan"]["Node Type"] == "Merge Join"
    assert plan["Plan"]["Actual Rows"] == 99
    entry, = feedback_entries(db)
    assert entry["last_actual_rows"] == 99
    trace = []
    _, hits = explain(db, query, settings=settings, trace=trace)
    assert hits == 1 and "scope=pair" in trace[0]


def multi_batch_hash_inputs(db):
    # Use small work memory so the hash join has to process multiple batches.
    query = "SELECT x.id, y.label FROM correlated x JOIN lookup_large y ON x.id=y.id"
    settings = """SET LOCAL work_mem='64kB';
SET LOCAL enable_nestloop=off;
SET LOCAL enable_mergejoin=off;"""
    plan, _ = explain(db, query, mode="collect", analyze=True, settings=settings)
    root = plan["Plan"]
    hash_node = next(node for node in nodes(root) if node["Node Type"] == "Hash")
    assert root["Node Type"] == "Hash Join"
    assert hash_node["Hash Batches"] > 1
    counts = sorted(entry["last_actual_rows"] for entry in feedback_entries(db))
    assert counts == sorted([root["Actual Rows"], hash_node["Actual Rows"]])


def inequality_join(db):
    # Check that a non-equality join can still reuse its query-specific count.
    clause = "FROM lookup_one a JOIN lookup_two b ON a.id > b.id WHERE a.id <= 10 AND b.id <= 10"
    seed = f"SELECT a.id, b.id {clause}"
    learned, _ = explain(db, seed, mode="collect", analyze=True)
    assert cardinality(learned)[1] == 45

    trace = []
    plan, _ = explain(db, seed, analyze=True, trace=trace)
    assert cardinality(plan)[:2] == (45, 45)
    assert any("scope=query" in line for line in trace)
    assert not any("scope=pair" in line for line in trace)

    larger = ("SELECT a.id, b.id FROM lookup_one a JOIN lookup_two b ON a.id > b.id "
              "JOIN correlated c ON c.id=a.id WHERE a.id <= 10 AND b.id <= 10")
    trace = []
    explain(db, larger, trace=trace)
    assert not any("scope=pair" in line or "scope=query" in line for line in trace)


def self_join(db):
    # A self-join has two aliases for one table ID, so it uses the query-specific key.
    query = ("SELECT a.id FROM lookup_one a JOIN lookup_one b ON a.id=b.id "
             "WHERE a.id < 10")
    plan, _ = explain(db, query, mode="collect", analyze=True)
    assert cardinality(plan)[1] == 9
    # The repeated self-join can hit without using a two-table transfer key.
    trace = []
    hot, _ = explain(db, query, analyze=True, trace=trace)
    assert cardinality(hot)[:2] == (9, 9)
    assert any("scope=query" in line for line in trace)
    assert not any("scope=pair" in line for line in trace)


def compound_join(db):
    # Swap both relations and clauses to test the two-table key's canonical order.
    seed = """SELECT a.id FROM lookup_one a JOIN lookup_two b
ON a.id=b.id AND a.label=b.label WHERE a.id < 70"""
    target = """SELECT a.id FROM lookup_two b JOIN lookup_one a
ON b.label=a.label AND b.id=a.id JOIN correlated c ON c.id=a.id
WHERE a.id < 70"""
    observed, _ = explain(db, seed, mode="collect", analyze=True)
    learned = cardinality(observed)[1]
    assert learned > 0
    trace = []
    explain(db, target, trace=trace)
    pairs = [line for line in trace if "scope=pair" in line]
    assert len(pairs) == 1 and f"learned={learned} " in pairs[0], trace

    changed = target.replace("b.label=a.label", "b.label=a.label + 1")
    trace = []
    explain(db, changed, trace=trace)
    assert not any("scope=pair" in line for line in trace), trace


def concurrent_distinct_keys(db):
    # Each worker gets its own range of keys to check for missed insertions.
    def learn_group(worker):
        first = worker * 10 + 1
        statements = [f"SELECT id FROM correlated WHERE id={value};" for value in range(
            first,
            first + 10,
        )]
        db.sql(transaction("collect", "\n".join(statements)))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(learn_group, range(4)))
    entries = feedback_entries(db)
    assert len(entries) == 40 and all(entry["observations"] == 1 for entry in entries)


def reset_during_reads(db):
    # Readers should see the entire old store or the empty store after reset.
    statements = [f"SELECT id FROM correlated WHERE id={value};" for value in range(1, 65)]
    db.sql(transaction("collect", "\n".join(statements)))
    assert len(feedback_entries(db)) == 64

    def sizes():
        return [len(feedback_entries(db)) for _ in range(3)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        readers = [pool.submit(sizes) for _ in range(3)]
        reset = pool.submit(db.sql, RESET)
        assert reset.result().stdout.strip() == "64"
        assert all(size in (0, 64) for reader in readers for size in reader.result())
    assert not feedback_entries(db)


def parameterized_scan(db):
    # Make the inner index scan execute once, with a value from its outer row.
    query = """SELECT s.join_id, d.id FROM cs631_verify.one_lookup s
JOIN lookup_one d ON d.id=s.join_id WHERE s.active"""
    settings = """SET LOCAL enable_mergejoin=off;
SET LOCAL enable_hashjoin=off;
SET LOCAL enable_material=off;"""
    learned, _ = explain(db, query, mode="collect", analyze=True, settings=settings)
    plan = learned["Plan"]
    assert plan["Node Type"] == "Nested Loop" and plan["Actual Rows"] == 1
    inner = next(node for node in nodes(plan) if node.get("Relation Name") == "lookup_one")
    assert inner["Actual Loops"] == 1 and "join_id" in inner["Index Cond"]

    # This scan has no outer key and can return the entire lookup table.
    plain = "SELECT id FROM lookup_one"
    cold, hits = explain(db, plain, analyze=True)
    assert hits == 0 and cardinality(cold)[1] == 100000
    trace = []
    explain(db, query, settings=settings, trace=trace)
    assert any("learned=1 scope=pair" in line for line in trace)


def four_table_query(db):
    # Add another table without changing the conditions on the trained pair.
    explain(db, DEMO["join2"], mode="collect", analyze=True)
    four_tables = DEMO["join3"].replace(
        "WHERE", "JOIN cs631_verify.sample s ON s.id=c.id WHERE", 1,
    )
    trace = []
    explain(db, four_tables, trace=trace)
    pair_hits = [line for line in trace if "scope=pair" in line]
    assert len(pair_hits) == 1 and "learned=1000" in pair_hits[0]
    assert result_hash(db, four_tables) == result_hash(db, four_tables, mode="disabled")


def private_pair(db):
    # Seed and apply the pair in one pinned transaction, then try a new connection.
    with sql_session(db) as writer:
        read_until_marker(writer, "BEGIN; UPDATE correlated SET b=-1 WHERE id=7;")
        # Move xmax ahead of the writer's still-running transaction ID.
        db.sql("CREATE TEMP TABLE advance_pair_scope (id int);")
        statements = (f"EXPLAIN (ANALYZE, FORMAT JSON) {DEMO['join2']};\n"
                      f"EXPLAIN (FORMAT JSON) {DEMO['join3']};")
        inside = db.sql(transaction("learned", statements, debug=True))
        assert inside.stderr.count("scope=pair") == 1
        _, hits = explain(db, DEMO["join3"])
        assert hits == 0


def physical_rewrite(db):
    # Keep the exported snapshot fixed so the storage locator decides the miss.
    query = "SELECT id FROM cs631_verify.mutations WHERE id > 0"
    identify = """SELECT oid, pg_relation_filenode(oid) FROM pg_class
WHERE oid='cs631_verify.mutations'::regclass;"""
    explain(db, query, mode="collect", analyze=True)
    old_oid, old_file = db.sql(identify).stdout.strip().split("|")
    with sql_session(db) as exporter:
        exported = read_until_marker(exporter, """BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_export_snapshot();""").strip()
        import_snapshot = f"SET TRANSACTION SNAPSHOT '{exported}';"
        _, hits = explain(db, query, settings=import_snapshot)
        assert hits == 1

        # The snapshot was exported without reading this table, so there is
        # no table lock blocking VACUUM FULL in the other connection.
        db.sql("VACUUM FULL cs631_verify.mutations;")
        new_oid, new_file = db.sql(identify).stdout.strip().split("|")
        assert old_oid == new_oid and old_file != new_file
        _, hits = explain(db, query, settings=import_snapshot)
        assert hits == 0


def diagnostic_average(db):
    # Use autocommit so these counts are only for inspection.
    query = "SELECT id FROM cs631_verify.mutations WHERE id > 0"
    diagnostic = "SET enable_card_feedback_collect=on;\n" + query + ";"
    db.sql(diagnostic)
    entry, = feedback_entries(db)
    assert entry["observations"] == 1 and entry["ema_actual_rows"] == 3
    try:
        db.sql("INSERT INTO cs631_verify.mutations VALUES (4), (5);")
        db.sql(diagnostic)
        updated, = feedback_entries(db)
        assert updated["signature"] == entry["signature"]
        assert updated["observations"] == 2 and updated["last_actual_rows"] == 5
        assert updated["ema_actual_rows"] == 4

        plan, hits = explain(db, query, analyze=True)
        assert hits == 0 and cardinality(plan)[1] == 5
    finally:
        db.sql("DELETE FROM cs631_verify.mutations WHERE id > 3;")


def install_fixtures(db):
    # Keep temporary test objects in dedicated schemas so cleanup is repeatable.
    db.sql("""
CREATE SCHEMA cs631_verify;
CREATE SCHEMA cs631_verify_other;

CREATE TABLE cs631_verify.sample (
    id int PRIMARY KEY,
    a int,
    flag boolean,
    label text
);
INSERT INTO cs631_verify.sample
SELECT i, i % 16,
       CASE WHEN i % 11 = 0 THEN NULL ELSE i % 2 = 0 END,
       CASE WHEN i % 17 = 0 THEN NULL ELSE 'group-' || (i % 13) END
FROM generate_series(1, 2048) AS g(i);
VACUUM ANALYZE cs631_verify.sample;

CREATE UNLOGGED TABLE cs631_verify.unlogged_sample AS
SELECT id FROM cs631_verify.sample;
CREATE TABLE cs631_verify.partitioned_sample (id int) PARTITION BY RANGE (id);
CREATE TABLE cs631_verify.sample_low PARTITION OF cs631_verify.partitioned_sample
FOR VALUES FROM (0) TO (1024);
CREATE TABLE cs631_verify.sample_rest PARTITION OF cs631_verify.partitioned_sample DEFAULT;
INSERT INTO cs631_verify.partitioned_sample SELECT id FROM cs631_verify.sample;

CREATE TABLE cs631_verify.inherited_sample (id int);
CREATE TABLE cs631_verify.inherited_child () INHERITS (cs631_verify.inherited_sample);
INSERT INTO cs631_verify.inherited_child SELECT id FROM cs631_verify.sample;

CREATE TABLE cs631_verify.identical_name AS SELECT generate_series(1, 13) AS id;
CREATE TABLE cs631_verify_other.identical_name AS SELECT generate_series(1, 5) AS id;
CREATE TABLE cs631_verify.mutations AS SELECT generate_series(1, 3) AS id;
CREATE TABLE cs631_verify.replacement AS SELECT generate_series(1, 2) AS id;
CREATE TABLE cs631_verify.one_lookup (join_id int, active bool);
INSERT INTO cs631_verify.one_lookup VALUES (7, true), (8, false);
ANALYZE cs631_verify.one_lookup;

CREATE TABLE cs631_verify.secure AS SELECT generate_series(1, 30) AS id;
ALTER TABLE cs631_verify.secure ENABLE ROW LEVEL SECURITY;
CREATE POLICY visible_rows ON cs631_verify.secure
USING (id <= current_setting('cs631_verify.cutoff')::int);

CREATE FUNCTION cs631_verify.is_seven(value int) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN RETURN value = 7; END; $$;
""")


def remove_fixtures(db):
    # Remove the tables and functions even after a failing check.
    db.sql("DROP SCHEMA IF EXISTS cs631_verify, cs631_verify_other CASCADE;")


CHECKS = (
    (switches, "the two switches worked independently, and plain EXPLAIN recorded nothing."),
    (shared_scan, 'independent read-only connections learned 10 → 1,000 with equal results.'),
    (in_flight_writer, 'a snapshot with an in-flight writer learned only within its transaction.'),
    (role_isolation, "a different role could not use the original role's observation."),
    (update_and_analyze, 'committed UPDATE and ANALYZE caused misses and allowed fresh learning.'),
    (truncated_table, 'TRUNCATE discarded the old count and learned an empty result.'),
    (repeated_joins, 'two- and three-table joins learned exact counts. The two-table join switched algorithms.'),
    (scan_transfer, 'a new join reused both standalone scans and missed on a different table or filter.'),
    (pair_transfer, 'a larger join reused a two-table count and missed when its effective conditions changed.'),
    (partial_and_prepared, 'partial plans, cursors and prepared plans supplied no reusable row counts.'),
    (partial_join_inputs, 'inner-unique nested-loop early stop and an empty hash build excluded partial children.'),
    (memoized_input, 'a completed join was learned above Memoize without learning the cached input.'),
    (concurrent_counts, 'four concurrent connections stored all 20 observations.'),
    (eviction, 'the 1,024-entry store evicted the least recently updated observation.'),
    (transaction_modes, "autocommit, read committed, read-write, and serializable did not apply feedback."),
    (parallel_settings, "enabling parallel workers blocked reuse for a serial plan."),
    (access_paths, "sequential, index, index-only, and bitmap scans shared complete counts."),
    (count_below_aggregate, "aggregation recorded the scan count separately from the number of groups."),
    (supported_filters, "arrays, null tests, Boolean filters, integer abs, and integer casts reused counts."),
    (changed_predicate, "changing a column, operator, or Boolean condition caused a miss."),
    (excluded_query_forms, "unsupported query forms kept their results without applying feedback."),
    (excluded_relation_kinds, "temporary, unlogged, sampled, partitioned, and inherited scans supplied no reusable hits."),
    (excluded_functions, "stable and user-defined filter functions did not supply reusable counts."),
    (large_key_fallback, "an oversized identity fell back, and an ordinary predicate still learned."),
    (same_name_in_other_schema, "same-named tables in different schemas kept separate observations."),
    (row_security, "row security returned each policy's rows without reusing feedback."),
    (scoped_reset, "a role reset only its own observations."),
    (database_scope, "inspection and reset stayed within the current database."),
    (insert_and_delete, "committed INSERT and DELETE invalidated prior counts and allowed relearning."),
    (fixed_snapshot, "an open repeatable-read transaction kept its old count across a concurrent commit."),
    (relation_replacement, "a replacement table with the same name learned a new count."),
    (bound_parameters, "generic and custom prepared plans skipped reuse with bound parameters."),
    (exhausted_portal, "a cursor exhausted in multiple calls supplied no reusable count."),
    (merge_join_inputs, "a merge join recorded its output without using its children's counts."),
    (multi_batch_hash_inputs, "a multi-batch hash join learned its output and build input only."),
    (inequality_join, "a non-equality join reused its count in the original query context."),
    (self_join, "a self-join kept its count in the original query context."),
    (compound_join, "multiple equality clauses matched after reordering and missed after a change."),
    (concurrent_distinct_keys, "four clients recorded distinct keys without dropping observations."),
    (reset_during_reads, "concurrent readers saw complete snapshots before or after a reset."),
    (parameterized_scan, "an inner scan executed once with a join key did not teach the unconditional scan."),
    (four_table_query, "a four-table query reused the matching two-table count."),
    (private_pair, "a pair count learned with an active writer stayed within its transaction."),
    (physical_rewrite, "an imported snapshot rejected a count after the physical table was rewritten."),
    (diagnostic_average, "the diagnostic average updated across observations without supplying reusable feedback."),
)


def verify(run):
    # Reset the shared store before each check so tests do not depend on order.
    _, db = prepare(run, "verify")
    with db:
        remove_fixtures(db)
        try:
            install_fixtures(db)
            # Give each check an empty store and report which one passed.
            for check, description in CHECKS:
                db.sql(RESET)
                check(db)
                print(f"PASS: {description}", flush=True)
        finally:
            remove_fixtures(db)
        explain(db, mode="collect", analyze=True)
        assert feedback_entries(db)

    # Start a second server process to verify that feedback was in memory only.
    with db:
        assert not feedback_entries(db)
    print("PASS: server restart cleared feedback.")


if __name__ == "__main__":
    verify(Path(sys.argv[1]))
