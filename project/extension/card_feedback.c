#include "postgres.h"

#include "fmgr.h"
#include "funcapi.h"
#include "optimizer/card_feedback.h"
#include "utils/builtins.h"
#include "utils/timestamp.h"

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
		Datum values[6];
		bool nulls[6] = {false};
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
		tuplestore_putvalues(rsinfo->setResult, rsinfo->setDesc, values, nulls);
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
