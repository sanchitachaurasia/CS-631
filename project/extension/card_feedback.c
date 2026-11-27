#include "postgres.h"

#include "fmgr.h"
#include "funcapi.h"
#include "optimizer/card_feedback.h"
#include "utils/builtins.h"
#include "utils/timestamp.h"
#include <math.h>

PG_MODULE_MAGIC;
PG_FUNCTION_INFO_V1(cf_entries);
PG_FUNCTION_INFO_V1(cf_reset);

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
		Datum values[8];
		bool nulls[8] = {false};
		char signature[32];
		double variance;
		double confidence;

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
		variance = entries[i].ema_log_rows_sq -
			entries[i].ema_log_rows * entries[i].ema_log_rows;
		if (variance < 0.0)
			variance = 0.0;
		confidence = (double) entries[i].observations /
			((double) entries[i].observations + 1.0);
		confidence *= 1.0 / (1.0 + sqrt(variance));
		values[6] = Float8GetDatum(confidence);
		values[7] = Float8GetDatum(sqrt(variance));
		tuplestore_putvalues(rsinfo->setResult,
							 rsinfo->setDesc,
							 values,
							 nulls);
	}
	pfree(entries);
	return (Datum) 0;
}

// clear the saved counts and tell the caller how many entries were removed.
Datum
cf_reset(PG_FUNCTION_ARGS)
{
	PG_RETURN_INT32(CardFeedbackReset());
}
