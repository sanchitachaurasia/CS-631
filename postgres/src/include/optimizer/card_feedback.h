#ifndef CARD_FEEDBACK_H
#define CARD_FEEDBACK_H

#include "nodes/pathnodes.h"
#include "datatype/timestamp.h"

extern uint64 card_feedback_rel_signature(PlannerInfo *root, RelOptInfo *rel);

#define CARD_FEEDBACK_MAX_ENTRIES 1024
#define CARD_FEEDBACK_HISTORY_SIZE 16
// leave room for a longer label when a query has more filters.
#define CARD_FEEDBACK_IDENTITY_BYTES 16384

extern PGDLLIMPORT bool enable_card_feedback_apply;
extern int card_feedback_cached_plan_depth;
extern char *CardFeedbackReadonlyIdentity(PlannerInfo *root, RelOptInfo *rel, List *restrictlist);
extern bool CardFeedbackLookup(uint64 signature, const char *identity, double *rows,
							   double *confidence);

typedef struct CardFeedbackKey
{
	Oid database_id;
	Oid user_id;
	uint64 signature;
} CardFeedbackKey;

typedef struct CardFeedbackObservation
{
	uint64 observation_id;
	double raw_est_rows;
	double actual_rows;
	TimestampTz observed_at;
} CardFeedbackObservation;

typedef struct CardFeedbackEntry
{
	CardFeedbackKey key;
	double raw_est_rows;
	double last_actual_rows;
	double ema_actual_rows;
	double recent_median_rows;
	double recent_median_q_error;
	double recent_p90_q_error;
	double recent_log_stddev;
	double recent_log_mad;
	double recent_log_slope;
	double drift_ratio;
	double confidence;
	uint8 recent_outlier_count;
	int8 trend_direction;
	uint64 observations;
	TimestampTz last_updated;
	uint8 history_next;
	uint8 history_count;
	CardFeedbackObservation history[CARD_FEEDBACK_HISTORY_SIZE];
	char identity[CARD_FEEDBACK_IDENTITY_BYTES];
} CardFeedbackEntry;

typedef struct CardFeedbackHistoryRow
{
	uint64 signature;
	uint64 observation_id;
	double raw_est_rows;
	double actual_rows;
	TimestampTz observed_at;
} CardFeedbackHistoryRow;

extern Size CardFeedbackShmemSize(void);
extern void CardFeedbackShmemInit(void);
extern void CardFeedbackRecord(uint64 signature, double raw_rows, double actual_rows,
							   const char *identity);
extern CardFeedbackEntry *CardFeedbackSnapshot(int *count);
extern CardFeedbackHistoryRow *CardFeedbackHistorySnapshot(int *count);
extern int CardFeedbackReset(void);
extern int CardFeedbackPrune(int64 max_age_usec);

#endif
