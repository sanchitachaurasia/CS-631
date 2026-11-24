-- One filter is easy. Two perfectly correlated filters are underestimated.
CREATE TABLE correlated (id int PRIMARY KEY, a int, b int);
INSERT INTO correlated
SELECT i, i % 100, i % 100
FROM generate_series(1, 100000) AS g(i);

-- Primary keys make the small join fixtures predictable.
CREATE TABLE lookup_one (id int PRIMARY KEY, label int);
CREATE TABLE lookup_two (id int PRIMARY KEY, label int);
INSERT INTO lookup_one SELECT i, i % 10 FROM generate_series(1, 100000) AS g(i);
INSERT INTO lookup_two SELECT i, i % 20 FROM generate_series(1, 100000) AS g(i);

-- Use a large statistics target so the demo's baseline estimate is stable.
ALTER TABLE correlated ALTER COLUMN a SET STATISTICS 1000;
ALTER TABLE correlated ALTER COLUMN b SET STATISTICS 1000;

-- Larger join with five correlated predicates. A permutation scatters index probes.
CREATE TABLE fact (id int, a int, b int, c int, d int, e int);
INSERT INTO fact
SELECT (i * 991::bigint % 500000)::int + 1, v, v, v, v, v
FROM generate_series(1, 500000) AS g(i)
CROSS JOIN LATERAL (SELECT i % 10 AS v) AS bucket;

-- Give the larger example an indexed inner input.
CREATE TABLE lookup_large (id int PRIMARY KEY, label int);
INSERT INTO lookup_large SELECT i, i % 20 FROM generate_series(1, 500000) AS g(i);
ANALYZE;
