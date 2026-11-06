#ifndef CARD_FEEDBACK_H
#define CARD_FEEDBACK_H

#include "nodes/pathnodes.h"
#include "datatype/timestamp.h"

extern uint64 card_feedback_rel_signature(PlannerInfo *root, RelOptInfo *rel);

#define CARD_FEEDBACK_MAX_ENTRIES 1024
// leave room for a longer label when a query has more filters.
#define CARD_FEEDBACK_IDENTITY_BYTES 16384

extern PGDLLIMPORT bool enable_card_feedback_apply;
extern int card_feedback_cached_plan_depth;
extern char *CardFeedbackReadonlyIdentity(PlannerInfo *root, RelOptInfo *rel);
extern bool CardFeedbackLookup(uint64 signature, const char *identity, double *rows);

typedef struct CardFeedbackKey
{
	Oid database_id;
	Oid user_id;
	uint64 signature;
} CardFeedbackKey;

typedef struct CardFeedbackEntry
{
	CardFeedbackKey key;
	double raw_est_rows;
	double last_actual_rows;
	double ema_actual_rows;
	uint64 observations;
	TimestampTz last_updated;
	char identity[CARD_FEEDBACK_IDENTITY_BYTES];
} CardFeedbackEntry;

extern Size CardFeedbackShmemSize(void);
extern void CardFeedbackShmemInit(void);
extern void CardFeedbackRecord(uint64 signature, double raw_rows, double actual_rows,
							   const char *identity);
extern CardFeedbackEntry *CardFeedbackSnapshot(int *count);
extern int CardFeedbackReset(void);

#endif
