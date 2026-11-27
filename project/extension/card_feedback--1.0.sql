\echo Use "CREATE EXTENSION card_feedback" to load this file. \quit

-- Show the recent estimate and observed counts for the current database and role.
CREATE FUNCTION card_feedback_entries(
    OUT signature text,
    OUT raw_est_rows double precision,
    OUT last_actual_rows double precision,
    OUT ema_actual_rows double precision,
    OUT observations bigint,
    OUT last_updated timestamptz,
    OUT feedback_confidence double precision,
    OUT log_row_stddev double precision
) RETURNS SETOF record
AS 'MODULE_PATHNAME', 'cf_entries'
LANGUAGE C VOLATILE PARALLEL UNSAFE;

-- Clear this database and role's entries and return how many were removed.
CREATE FUNCTION card_feedback_reset() RETURNS integer
AS 'MODULE_PATHNAME', 'cf_reset'
LANGUAGE C VOLATILE PARALLEL UNSAFE;
