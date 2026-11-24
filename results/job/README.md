# IMDb benchmark

The project uses the May 2013 IMDb snapshot prepared for the
[Join Order Benchmark](https://github.com/viktorleis/job). The benchmark
driver fetches the [CWI archive](https://event.cwi.nl/da/job/imdb.tgz)
and verifies its SHA-256 before extracting it.

```text
25f9d893c54f903366e0c263f88db0d429dbc2b159d4987ebc1e203242a7e988
```

The [query files, schema and indexes](https://github.com/gregrahn/join-order-benchmark/tree/a39603662e023e449cb2121997a5034df9e02ebf)
are pinned in the [driver](../../project/bench.py). The archive is about
1.2 GB. Allow about 25 GB for the download, extracted CSVs and two
database clusters. For a manual download, place the archive at
`.local/bench/imdb.tgz`. IMDb's [data-use terms](https://help.imdb.com/article/imdb/general-information/can-i-use-imdb-data-in-my-software/G5JTRESSHJBBHTGX)
apply to the data.

## Run from the repository root

Use macOS or Linux with a C toolchain, make, Perl, Bison, Flex, Git,
rsync, curl, tar and Python 3.11 or later.

```sh
bash project/run.sh build
bash project/run.sh verify
bash project/run.sh job quick
```

The first benchmark run downloads the data, applies the JOB schema
and indexes, and runs `VACUUM ANALYZE` before cloning the database
for the patched server. Both servers use the same data and settings.
Stock is PostgreSQL 16.13. Patched off has both feedback switches off,
collect only records counts, and learned uses those counts for planning.

To repeat the two groups in the report:

```sh
job_queries() {
    python3 -c 'import csv, sys; print(",".join(dict.fromkeys(r["query"] for r in csv.DictReader(sys.stdin))))' \
        < "results/job/job-$1.csv"
}

CS631_QUERIES="$(job_queries detailed)" CS631_REPETITIONS=3 \
    CS631_TIMEOUT_MS=15000 CS631_RUN_NAME=job-detailed bash project/run.sh job all
CS631_QUERIES="$(job_queries screen)" CS631_REPETITIONS=0 \
    CS631_TIMEOUT_MS=4000 CS631_RUN_NAME=job-screen bash project/run.sh job all
bash project/run.sh review transfer
bash project/run.sh review pair
```

New files go to `.local/measurements` by default. Set `CS631_OUTPUT`
to change that. For the store, repeat, timeout and snapshot experiments,
run `bash project/run.sh build profile` and `bash project/run.sh review`.

## Saved results

The CSVs summarize each query and mode. The compressed files preserve
settings, plans, checksums, durations and timeouts. The archived
detailed run used the engine from the **collect counts from completed
joins** commit. Its reported speedups cover the 22 of 28 queries that
finished in all four modes. The [corrected replay](corrected/) uses the
final patch and binary result comparisons. The [verification log](../verify.txt)
records the 46 project checks on the assertion-enabled build.
