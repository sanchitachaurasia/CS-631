\echo Use "CREATE EXTENSION card_feedback" to load this file. \quit

-- name the columns we show when someone asks for the saved counts.
CREATE FUNCTION card_feedback_entries(
    OUT signature text,
    OUT raw_est_rows double precision,
    OUT last_actual_rows double precision,
    OUT ema_actual_rows double precision,
    OUT observations bigint,
    OUT last_updated timestamptz,
    OUT feedback_confidence double precision,
    OUT log_row_stddev double precision,
    OUT recent_median_rows double precision,
    OUT recent_median_q_error double precision,
    OUT drift_ratio double precision,
    OUT recent_outlier_count integer,
    OUT recent_p90_q_error double precision,
    OUT recent_log_slope double precision,
    OUT trend_direction integer
) RETURNS SETOF record
AS 'MODULE_PATHNAME', 'cf_entries'
LANGUAGE C VOLATILE PARALLEL UNSAFE;

-- Return the bounded recent observation ring for the current database and role.
CREATE FUNCTION card_feedback_history(
    OUT signature text,
    OUT observation_id bigint,
    OUT observed_at timestamptz,
    OUT raw_est_rows double precision,
    OUT actual_rows double precision
) RETURNS SETOF record
AS 'MODULE_PATHNAME', 'cf_history'
LANGUAGE C VOLATILE PARALLEL UNSAFE;

-- call the c function that clears the saved counts.
CREATE FUNCTION card_feedback_reset() RETURNS integer
AS 'MODULE_PATHNAME', 'cf_reset'
LANGUAGE C VOLATILE PARALLEL UNSAFE;

-- Remove entries whose last observation is older than the supplied age.
CREATE FUNCTION card_feedback_prune(older_than_seconds integer) RETURNS integer
AS 'MODULE_PATHNAME', 'cf_prune'
LANGUAGE C VOLATILE PARALLEL UNSAFE STRICT;
