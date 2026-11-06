\echo Use "CREATE EXTENSION card_feedback" to load this file. \quit

-- name the columns we show when someone asks for the saved counts.
CREATE FUNCTION card_feedback_entries(
    OUT signature text,
    OUT raw_est_rows double precision,
    OUT last_actual_rows double precision,
    OUT ema_actual_rows double precision,
    OUT observations bigint,
    OUT last_updated timestamptz
) RETURNS SETOF record
AS 'MODULE_PATHNAME', 'cf_entries'
LANGUAGE C VOLATILE PARALLEL UNSAFE;

-- call the c function that clears the saved counts.
CREATE FUNCTION card_feedback_reset() RETURNS integer
AS 'MODULE_PATHNAME', 'cf_reset'
LANGUAGE C VOLATILE PARALLEL UNSAFE;
