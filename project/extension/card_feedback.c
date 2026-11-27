#include "postgres.h"

#include "fmgr.h"
#include "funcapi.h"
#include "optimizer/card_feedback.h"
#include "utils/builtins.h"
#include "utils/timestamp.h"

PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(cf_entries);
PG_FUNCTION_INFO_V1(cf_history);
PG_FUNCTION_INFO_V1(cf_reset);
PG_FUNCTION_INFO_V1(cf_prune);

// show the saved counts as rows in sql.
Datum
cf_entries(PG_FUNCTION_ARGS)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	CardFeedbackEntry *entries;
	int count;
	int i;

	// copy the entries first so other connections can use the store while we
	// build the output.
	InitMaterializedSRF(fcinfo, 0);
	entries = CardFeedbackSnapshot(&count);
	// make one row for each saved count.
	for (i = 0; i < count; i++)
	{
		Datum values[15];
		bool nulls[15] = {false};
		char signature[32];

		// show the short id as text, it might be too big for a signed number
		// in sql.
		snprintf(signature, sizeof(signature), "%llu",
				 (unsigned long long) entries[i].key.signature);
		values[0] = CStringGetTextDatum(signature);
		// fill in the estimate, what we counted, and the other columns.
		values[1] = Float8GetDatum(entries[i].raw_est_rows);
		values[2] = Float8GetDatum(entries[i].last_actual_rows);
		values[3] = Float8GetDatum(entries[i].ema_actual_rows);
		values[4] = Int64GetDatum((int64) entries[i].observations);
		values[5] = TimestampTzGetDatum(entries[i].last_updated);
		values[6] = Float8GetDatum(entries[i].confidence);
		values[7] = Float8GetDatum(entries[i].recent_log_stddev);
		values[8] = Float8GetDatum(entries[i].recent_median_rows);
		values[9] = Float8GetDatum(entries[i].recent_median_q_error);
		values[10] = Float8GetDatum(entries[i].drift_ratio);
		values[11] = Int32GetDatum(entries[i].recent_outlier_count);
		values[12] = Float8GetDatum(entries[i].recent_p90_q_error);
		values[13] = Float8GetDatum(entries[i].recent_log_slope);
		values[14] = Int32GetDatum(entries[i].trend_direction);
		tuplestore_putvalues(rsinfo->setResult,
							 rsinfo->setDesc,
							 values,
							 nulls);
	}
	pfree(entries);
	return (Datum) 0;
}

// Return each retained observation so callers can inspect feedback over time.
Datum
cf_history(PG_FUNCTION_ARGS)
{
	ReturnSetInfo *rsinfo = (ReturnSetInfo *) fcinfo->resultinfo;
	CardFeedbackHistoryRow *history;
	int count;
	int i;

	InitMaterializedSRF(fcinfo, 0);
	history = CardFeedbackHistorySnapshot(&count);
	for (i = 0; i < count; i++)
	{
		Datum values[5];
		bool nulls[5] = {false};
		char signature[32];

		snprintf(signature, sizeof(signature), "%llu",
				 (unsigned long long) history[i].signature);
		values[0] = CStringGetTextDatum(signature);
		values[1] = Int64GetDatum((int64) history[i].observation_id);
		values[2] = TimestampTzGetDatum(history[i].observed_at);
		values[3] = Float8GetDatum(history[i].raw_est_rows);
		values[4] = Float8GetDatum(history[i].actual_rows);
		tuplestore_putvalues(rsinfo->setResult,
							 rsinfo->setDesc,
							 values,
							 nulls);
	}
	pfree(history);
	return (Datum) 0;
}

// clear the saved counts and tell the caller how many entries were removed.
Datum
cf_reset(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT32(CardFeedbackReset());
}

// Delete feedback untouched for at least the requested number of seconds.
Datum
cf_prune(PG_FUNCTION_ARGS)
{
	int32 older_than_seconds = PG_GETARG_INT32(0);
	int64 older_than_usec;

	if (older_than_seconds < 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("feedback age must not be negative")));
	older_than_usec = (int64) older_than_seconds * USECS_PER_SEC;
	PG_RETURN_INT32(CardFeedbackPrune(older_than_usec));
}
