/* save complete row counts and use them when postgresql picks a plan. */
#include "postgres.h"

#include "common/hashfn.h"
#include "access/xact.h"
#include "catalog/pg_am_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_operator_d.h"
#include "catalog/pg_proc_d.h"
#include "catalog/pg_type_d.h"
#include "nodes/nodeFuncs.h"
#include "optimizer/cost.h"
#include "storage/proc.h"
#include "utils/fmgroids.h"
#include "utils/lsyscache.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "nodes/print.h"
#include "optimizer/card_feedback.h"
#include "optimizer/clauses.h"
#include "parser/parsetree.h"
#include "storage/lwlock.h"
#include "storage/shmem.h"
#include "utils/hsearch.h"
#include "utils/rel.h"
#include "utils/snapmgr.h"
#include "utils/timestamp.h"
#include <float.h>
#include <math.h>

// Treat five percent log-count movement per sample as a directional trend.
#define CARD_FEEDBACK_TREND_LOG_THRESHOLD 0.05

#ifdef CARD_FEEDBACK_PROFILE
#include "portability/instr_time.h"
#include "utils/guc.h"

// show how long it took to get or save a count, including how long we waited
// for the lock.
static void
profile_store(const char *op, instr_time start, instr_time acquired,
			  instr_time finished, bool hit)
{
	instr_time elapsed = finished;
	instr_time acquire = acquired;
	static bool reported_size = false;

	// subtract the two times to see how long the work and the wait took.
	INSTR_TIME_SUBTRACT(elapsed, start);
	INSTR_TIME_SUBTRACT(acquire, start);
	// show how much space the store and each entry use, once per connection.
	if (!reported_size)
	{
		elog(DEBUG2, "card_feedback profile bytes=%zu entry=%zu",
			 CardFeedbackShmemSize(), sizeof(CardFeedbackEntry));
		reported_size = true;
	}
	elog(DEBUG2, "card_feedback profile op=%s ns=" UINT64_FORMAT
		 " acquire_ns=" UINT64_FORMAT " hit=%d", op,
		 INSTR_TIME_GET_NANOSEC(elapsed), INSTR_TIME_GET_NANOSEC(acquire), hit);
}
#endif

bool enable_card_feedback_apply = false;
int card_feedback_cached_plan_depth = 0;

// use built-in functions that postgresql marks as giving the same answer for
// the same input.
static bool
safe_function(Oid function)
{
	return OidIsValid(function) && function < FirstGenbkiObjectId &&
		func_volatile(function) == PROVOLATILE_IMMUTABLE;
}

/*
 * Catalog immutability alone is not a session-independence guarantee.
 * For example, float8out() is marked immutable but uses extra_float_digits.
 * Only allow scalar functions whose behavior is covered by the key.
 */
static bool
safe_scalar_function(Oid function)
{
	switch (function)
	{
		case F_ABS_INT2:
		case F_ABS_INT4:
		case F_ABS_INT8:
		case F_INT2_INT4:
		case F_INT2_INT8:
		case F_INT4_INT2:
		case F_INT4_INT8:
		case F_INT8_INT2:
		case F_INT8_INT4:
			return safe_function(function);
		default:
			return false;
	}
}

// use ordinary values like an integer or some text. a whole row can contain
// many different fields.
static bool
safe_type(Oid type)
{
	return OidIsValid(type) && type < FirstGenbkiObjectId &&
		!type_is_rowtype(type);
}

typedef struct FeedbackExprContext
{
	const Bitmapset *relids;
	Index first_table;			/* zero keeps the original range-table positions */
} FeedbackExprContext;

/* ignore where the filter was written, so select and explain can match. */
static bool
CardFeedbackNormalizeExpr(Node *node, void *context)
{
	FeedbackExprContext *expr = context;

	if (node == NULL)
		return false;
	switch (nodeTag(node))
	{
		case T_Var:
		{
			Var *var = (Var *) node;

			if (!bms_is_member(var->varno, expr->relids) || var->varlevelsup != 0 ||
				var->varattno <= 0 || !safe_type(var->vartype))
				return true;
			// call the two tables one and two in the label, so changing
			// their positions in the query will not change it.
			if (expr->first_table != 0)
			{
				// a left join can give us a null instead of the original
				// column value. skip making a key for sharing that count.
				if (!bms_is_empty(var->varnullingrels))
					return true;
				var->varno = var->varnosyn = var->varno == expr->first_table ? 1 : 2;
				var->varattnosyn = var->varattno;
			}
			var->location = -1;
			return false;
		}
		case T_Const:
		{
			Const *value = (Const *) node;

			if (!safe_type(value->consttype))
				return true;
			value->location = -1;
			return false;
		}
		// look up the code for an operator such as = or < before keeping the
		// comparison.
		case T_OpExpr:
		{
			OpExpr *op = (OpExpr *) node;

			op->opfuncid = get_opcode(op->opno);
			if (op->opno >= FirstGenbkiObjectId || !safe_function(op->opfuncid))
				return true;
			op->location = -1;
			break;
		}
		// where id in ('2', '3') can turn into one check against a list.
		// keep it so we can remember how many rows matched.
		case T_ScalarArrayOpExpr:
		{
			ScalarArrayOpExpr *op = (ScalarArrayOpExpr *) node;

			op->opfuncid = get_opcode(op->opno);
			if (op->opno >= FirstGenbkiObjectId || !safe_function(op->opfuncid))
				return true;
			op->location = -1;
			break;
		}
		case T_FuncExpr:
		{
			FuncExpr *func = (FuncExpr *) node;

			// a function that returns many rows can change the count. this
			// check only accepts a function that gives one value.
			if (func->funcretset || !safe_scalar_function(func->funcid))
				return true;
			func->location = -1;
			break;
		}
		case T_RelabelType:
			((RelabelType *) node)->location = -1;
			break;
		case T_ArrayExpr:
			((ArrayExpr *) node)->location = -1;
			break;
		case T_NullTest:
			((NullTest *) node)->location = -1;
			break;
		case T_BooleanTest:
			((BooleanTest *) node)->location = -1;
			break;
		case T_BoolExpr:
			((BoolExpr *) node)->location = -1;
			break;
		case T_RangeTblRef:
			return !bms_is_member(((RangeTblRef *) node)->rtindex, expr->relids);
		case T_FromExpr:
		{
			FromExpr *from = (FromExpr *) node;
			ListCell *lc;

			foreach(lc, from->fromlist)
				if (CardFeedbackNormalizeExpr(lfirst(lc), context))
					return true;
			return CardFeedbackNormalizeExpr(from->quals, context);
		}
		case T_JoinExpr:
		{
			JoinExpr *join = (JoinExpr *) node;

			return join->jointype != JOIN_INNER ||
				CardFeedbackNormalizeExpr(join->larg, context) ||
				CardFeedbackNormalizeExpr(join->rarg, context) ||
				CardFeedbackNormalizeExpr(join->quals, context);
		}
		case T_List:
			break;
		default:
			return true;
	}
	return expression_tree_walker(node, CardFeedbackNormalizeExpr, context);
}

// save which version of the data we counted, so an old count cannot match a
// different view of the table.
static void
append_snapshot_scope(StringInfo key)
{
	// ask postgresql which rows this query is allowed to see.
	Snapshot snapshot = ActiveSnapshotSet() ? GetActiveSnapshot() : NULL;
#ifdef CARD_FEEDBACK_PROFILE
	const char *local_scope = GetConfigOption("card_feedback.local_scope", true, false);

	// for this test, keep counts in one transaction so we can compare with
	// sharing turned on.
	if (local_scope != NULL && strcmp(local_scope, "on") == 0)
		snapshot = NULL;
#endif

	// if no older transaction is still running, this cutoff can identify the
	// same view for another connection.
	if (snapshot != NULL && snapshot->snapshot_type == SNAPSHOT_MVCC &&
		snapshot->xmin == snapshot->xmax && !snapshot->takenDuringRecovery &&
		TransactionIdIsNormal(snapshot->xmax))
	{
		FullTransactionId next = ReadNextFullTransactionId();
		uint32 epoch = EpochFromFullTransactionId(next);

		// transaction numbers can repeat. keep track of the round so an old
		// number cannot match a new one.
		if (snapshot->xmax > XidFromFullTransactionId(next))
			epoch--;
		appendStringInfo(key, "shared:%u:%u:", epoch, snapshot->xmax);
	}
	else
		// otherwise only reuse the count in this connection and transaction.
		appendStringInfo(key, "local:%d:%lld:%u:", MyProcPid,
						 (long long) MyStartTimestamp, MyProc->lxid);
}

/* Freeze the whole supported query context before join search changes paths. */
static char *
CardFeedbackReadonlyContext(PlannerInfo *root)
{
	Query *query = root->parse;
	StringInfoData key;
	ListCell *lc;
	Bitmapset *allowed = NULL;
	FeedbackExprContext expr = {0};
	int rti = 0;
	Node *jointree;
	char *serialized;

	if (root->feedback_reuse_checked)
		return root->feedback_reuse_context;
	root->feedback_reuse_checked = true;
	if (!XactReadOnly || XactIsoLevel != XACT_REPEATABLE_READ ||
		!IsTransactionBlock() || TransactionIdIsValid(GetTopTransactionIdIfAny()) ||
		card_feedback_cached_plan_depth != 0 || root->glob->boundParams != NULL ||
		max_parallel_workers_per_gather != 0 || root->parent_root != NULL ||
		// order by can still use an input count. limit 10 might stop after
		// ten rows, so skip it.
		query->commandType != CMD_SELECT ||
		query->hasWindowFuncs || query->hasTargetSRFs ||
		query->hasSubLinks || query->hasModifyingCTE || query->hasRowSecurity ||
		query->cteList != NIL || query->groupingSets != NIL ||
		query->havingQual != NULL || query->limitCount != NULL ||
		query->limitOffset != NULL || query->rowMarks != NIL ||
		query->setOperations != NULL)
		return NULL;
	initStringInfo(&key);
	append_snapshot_scope(&key);
	foreach(lc, query->rtable)
	{
		RangeTblEntry *rte = lfirst_node(RangeTblEntry, lc);
		Relation relation;

		rti++;
		if (rte->rtekind == RTE_JOIN && rte->jointype == JOIN_INNER)
			continue;
		if (rte->rtekind != RTE_RELATION || rte->relkind != RELKIND_RELATION ||
			rte->tablesample != NULL || rte->securityQuals != NIL ||
			rte->relid < FirstNormalObjectId || has_subclass(rte->relid) ||
			get_rel_persistence(rte->relid) != RELPERSISTENCE_PERMANENT)
		{
			pfree(key.data);
			return NULL;
		}
		relation = RelationIdGetRelation(rte->relid);
		if (!RelationIsValid(relation) || relation->rd_rel->relam != HEAP_TABLE_AM_OID)
		{
			if (RelationIsValid(relation))
				RelationClose(relation);
			pfree(key.data);
			return NULL;
		}
		allowed = bms_add_member(allowed, rti);
		// include where the table is stored. if postgresql rewrites the
		// table, the old count should miss.
		appendStringInfo(&key, "table:%d:%u:%u:%u;", rti, rte->relid,
						 relation->rd_locator.spcOid, relation->rd_locator.relNumber);
		RelationClose(relation);
	}
	if (bms_is_empty(allowed))
	{
		pfree(key.data);
		return NULL;
	}
	// selecting fewer columns or sorting rows still uses the same input count.
	jointree = (Node *) copyObject(query->jointree);
	expr.relids = allowed;
	if (CardFeedbackNormalizeExpr(jointree, &expr))
	{
		pfree(key.data);
		return NULL;
	}
	serialized = nodeToString(jointree);
	appendStringInfo(&key, "tree:%s;", serialized);
	pfree(serialized);
	if (key.len >= CARD_FEEDBACK_IDENTITY_BYTES)
	{
		pfree(key.data);
		return NULL;
	}
	root->feedback_reuse_relids = allowed;
	root->feedback_reuse_context = key.data;
	return key.data;
}

// compare two filters as text so we can sort them.
static int
compare_clauses(const ListCell *a, const ListCell *b)
{
	return strcmp(lfirst(a), lfirst(b));
}

// put the length first, so 'ab' plus 'c' stays different from 'a' plus 'bc'.
static void
append_key_part(StringInfo key, const char *part)
{
	appendStringInfo(key, "%zu:%s", strlen(part), part);
}

// put the two sides of an equals check in the same order each time.
static bool
order_join_clause(RestrictInfo *restriction, Node *clause, Index first)
{
	OpExpr *op;
	Var *left;
	Var *right;

	// use an equals check like a.id = b.id to share a count.
	// a join on a.id < b.id can still use its count in the same query.
	if (restriction->mergeopfamilies == NIL || !IsA(clause, OpExpr))
		return false;
	// keep it simple, like a.id = b.id. an expression like a.id + 1 is not a
	// plain column.
	op = (OpExpr *) clause;
	if (op->opno >= FirstGenbkiObjectId || list_length(op->args) != 2 ||
		!IsA(linitial(op->args), Var) || !IsA(lsecond(op->args), Var))
		return false;

	left = linitial_node(Var, op->args);
	right = lsecond_node(Var, op->args);
	// a.id = a.other_id only uses a, so it does not connect the two tables.
	if (left->varno == right->varno)
		return false;
	if (left->varno != first)
	{
		// ask postgresql how to flip the check, so b.id = a.id matches a.id
		// = b.id.
		Oid commutator = get_commutator(op->opno);

		if (!OidIsValid(commutator) || commutator >= FirstGenbkiObjectId)
			return false;
		CommuteOpExpr(op);
	}
	return true;
}

// make the key, a label for all the filters used to count these rows.
static bool
append_restrictions(StringInfo key, List *restrictions,
					FeedbackExprContext *expr, bool join)
{
	List *clauses = NIL;
	ListCell *lc;

	// check each filter the planner chose for this scan or join.
	foreach(lc, restrictions)
	{
		RestrictInfo *restriction = lfirst_node(RestrictInfo, lc);
		// edit a copy to make the label. the query still needs the original
		// filter.
		Node *clause = (Node *) copyObject(restriction->clause);

		// a check like 1 = 0 has the same answer for every row. skip this
		// key if a check is like that or we cannot use one of its filters.
		if (restriction->pseudoconstant ||
			// a join needs a check like a.id = b.id here. a filter on one
			// table can still use id < 5.
			(join && !order_join_clause(restriction, clause, expr->first_table)) ||
			CardFeedbackNormalizeExpr(clause, expr))
		{
			// one filter we cannot use is enough to drop this label. free
			// the pieces we already made.
			list_free_deep(clauses);
			return false;
		}
		// turn the checked filter into text so it can be put in the label.
		clauses = lappend(clauses, nodeToString(clause));
	}
	// a = 2 and b = 3 matches b = 3 and a = 2.
	// the order here does not change which rows pass.
	list_sort(clauses, compare_clauses);
	// add each filter to the label in that order.
	foreach(lc, clauses)
		append_key_part(key, lfirst(lc));
	list_free_deep(clauses);
	return key->len < CARD_FEEDBACK_IDENTITY_BYTES;
}

// make a label for one table, the data we can see, and the filters we use.
static char *
CardFeedbackScanIdentity(PlannerInfo *root, RelOptInfo *rel)
{
	FeedbackExprContext expr = {rel->relids, rel->relid};
	// turn the table position in this query into the id of the table itself.
	Oid relid = planner_rt_fetch(rel->relid, root)->relid;
	Relation relation;
	StringInfoData key;

	// open the table so we can find where its data is stored.
	relation = RelationIdGetRelation(relid);
	if (!RelationIsValid(relation))
		return NULL;
	initStringInfo(&key);
	// start with scan so this count stays separate from counts for joins.
	appendStringInfoString(&key, "scan:");
	append_snapshot_scope(&key);
	appendStringInfo(&key, "%u:%u:%u:", relid,
					 relation->rd_locator.spcOid, relation->rd_locator.relNumber);
	RelationClose(relation);
	// include filters postgresql works out.
	// a.id = b.id and b.id = 3 can also give us a.id = 3.
	if (!append_restrictions(&key, rel->baserestrictinfo, &expr, false))
	{
		pfree(key.data);
		return NULL;
	}
	return key.data;
}

// make a label from the two table filters and the check that joins them.
static char *
CardFeedbackPairIdentity(PlannerInfo *root, RelOptInfo *rel, List *restrictlist)
{
	Index first;
	Index second;
	Oid first_oid;
	Oid second_oid;
	const char *left;
	const char *right;
	FeedbackExprContext expr;
	StringInfoData key;

	// share a count for two tables with a join condition. a cross join uses
	// the label for its own query.
	if (restrictlist == NIL || bms_num_members(rel->relids) != 2)
		return NULL;
	// find the ids of the two tables, even if they have different names in
	// this query.
	first = bms_next_member(rel->relids, -1);
	second = bms_next_member(rel->relids, first);
	first_oid = planner_rt_fetch(first, root)->relid;
	second_oid = planner_rt_fetch(second, root)->relid;
	// items a join items b on a.id = b.id uses the same table twice.
	// skip sharing this join count with other queries. it can still match
	// when the same query runs again.
	if (first_oid == second_oid)
		return NULL;
	// sort by table id so a join b can match b join a when the filters are
	// the same.
	if (first_oid > second_oid)
	{
		Index tmp = first;

		first = second;
		second = tmp;
	}
	// use the labels we made for each table, so both sets of filters are
	// part of this join label.
	left = root->simple_rel_array[first]->feedback_identity;
	right = root->simple_rel_array[second]->feedback_identity;
	// each table needs a label we can use on its own. a label tied to this
	// whole query cannot be shared.
	if (left == NULL || right == NULL ||
		strncmp(left, "scan:", 5) != 0 || strncmp(right, "scan:", 5) != 0)
		return NULL;

	expr.relids = rel->relids;
	expr.first_table = first;
	initStringInfo(&key);
	// start with pair so a join count cannot be mistaken for a count from
	// one table.
	appendStringInfoString(&key, "pair:");
	append_key_part(&key, left);
	append_key_part(&key, right);
	// a.id = b.id and a.code = b.code can match different rows.
	// include the join condition so they get different labels.
	if (!append_restrictions(&key, restrictlist, &expr, true))
	{
		pfree(key.data);
		return NULL;
	}
	return key.data;
}

char *
CardFeedbackReadonlyIdentity(PlannerInfo *root, RelOptInfo *rel, List *restrictlist)
{
	const char *context = CardFeedbackReadonlyContext(root);
	char *identity;
	StringInfoData key;
	int member = -1;

	if (context == NULL || bms_is_empty(rel->relids) ||
		!bms_is_subset(rel->relids, root->feedback_reuse_relids) ||
		(rel->reloptkind != RELOPT_BASEREL && rel->reloptkind != RELOPT_JOINREL))
		return NULL;
	// look for a count we can share first. if there is no way to name it for
	// sharing, use a label tied to this query.
	if (rel->reloptkind == RELOPT_BASEREL)
		identity = CardFeedbackScanIdentity(root, rel);
	else
		identity = CardFeedbackPairIdentity(root, rel, restrictlist);
	if (identity != NULL)
		return identity;
	initStringInfo(&key);
	appendStringInfo(&key, "%ssubset:", context);
	while ((member = bms_next_member(rel->relids, member)) >= 0)
		appendStringInfo(&key, "%d,", member);
	if (key.len >= CARD_FEEDBACK_IDENTITY_BYTES)
	{
		pfree(key.data);
		return NULL;
	}
	return key.data;
}

uint64
card_feedback_rel_signature(PlannerInfo *root, RelOptInfo *rel)
{
	StringInfoData key;
	int member = -1;
	uint64 result;

	/* Subquery/outer-join identities need a separate design. */
	if (root->parent_root != NULL || root->parse->commandType != CMD_SELECT ||
		root->parse->hasSubLinks || root->parse->hasModifyingCTE ||
		root->parse->hasRowSecurity || bms_is_empty(rel->relids))
		return 0;
	if (root->feedback_context == 0)
	{
		char *serialized = nodeToString(root->parse);

		root->feedback_context = hash_bytes_extended((unsigned char *) serialized,
												 strlen(serialized), 0);
		pfree(serialized);
	}
	initStringInfo(&key);
	appendStringInfo(&key, "%u:%u:%llu", MyDatabaseId, GetUserId(),
					 (unsigned long long) root->feedback_context);
	while ((member = bms_next_member(rel->relids, member)) >= 0)
	{
		RangeTblEntry *rte;

		if (member <= 0 || member >= root->simple_rel_array_size)
		{
			pfree(key.data);
			return 0;
		}
		rte = planner_rt_fetch(member, root);
		if (rte->rtekind != RTE_RELATION)
		{
			pfree(key.data);
			return 0;
		}
		appendStringInfo(&key, ":%d:%u", member, rte->relid);
	}
	result = hash_bytes_extended((unsigned char *) key.data, key.len, 0);
	pfree(key.data);
	return result;
}

/* counts stay in memory until removed, cleared, or the server restarts. */
static HTAB *CardFeedbackHash = NULL;

// Keep a bounded chronological sample for each reusable feedback identity.
static void
CardFeedbackRememberObservation(CardFeedbackEntry *entry, double raw_rows,
								double actual_rows, TimestampTz observed_at)
{
	CardFeedbackObservation *observation;
	int slot = entry->history_next;

	observation = &entry->history[slot];
	observation->observation_id = entry->observations;
	observation->raw_est_rows = raw_rows;
	observation->actual_rows = actual_rows;
	observation->observed_at = observed_at;

	entry->history_next = (slot + 1) % CARD_FEEDBACK_HISTORY_SIZE;
	if (entry->history_count < CARD_FEEDBACK_HISTORY_SIZE)
		entry->history_count++;
}

// Sort local metric vectors while leaving the observation ring in time order.
static int
CardFeedbackCompareDouble(const void *left, const void *right)
{
	double a = *((const double *) left);
	double b = *((const double *) right);

	if (a < b)
		return -1;
	if (a > b)
		return 1;
	return 0;
}

// Calculate a median after sorting a local copy of a metric vector.
static double
CardFeedbackMedian(double *values, int count)
{
	if (count <= 0)
		return 0.0;
	qsort(values, count, sizeof(double), CardFeedbackCompareDouble);
	if (count % 2 != 0)
		return values[count / 2];
	return values[count / 2 - 1] * 0.5 + values[count / 2] * 0.5;
}

// Return a nearest-rank percentile from a small local copy of recent errors.
static double
CardFeedbackPercentile(double *values, int count, double percentile)
{
	int rank;

	if (count <= 0)
		return 0.0;
	if (percentile < 0.0)
		percentile = 0.0;
	else if (percentile > 1.0)
		percentile = 1.0;
	qsort(values, count, sizeof(double), CardFeedbackCompareDouble);
	rank = (int) ceil(percentile * count) - 1;
	if (rank < 0)
		rank = 0;
	else if (rank >= count)
		rank = count - 1;
	return values[rank];
}

// Estimate direction from the ordered log-count sample, not a sorted copy.
static double
CardFeedbackLogSlope(const double *values, int count)
{
	double mean_x = (double) (count - 1) * 0.5;
	double mean_y = 0.0;
	double numerator = 0.0;
	double denominator = 0.0;
	int i;

	if (count < 2)
		return 0.0;
	for (i = 0; i < count; i++)
		mean_y += values[i] / count;
	for (i = 0; i < count; i++)
	{
		double x_delta = i - mean_x;

		numerator += x_delta * (values[i] - mean_y);
		denominator += x_delta * x_delta;
	}
	return denominator > 0.0 ? numerator / denominator : 0.0;
}

// Refresh robust recent-error, variability, drift, and confidence metrics.
static void
CardFeedbackRefreshSummary(CardFeedbackEntry *entry)
{
	double actual_rows[CARD_FEEDBACK_HISTORY_SIZE];
	double q_errors[CARD_FEEDBACK_HISTORY_SIZE];
	double log_rows[CARD_FEEDBACK_HISTORY_SIZE];
	double deviations[CARD_FEEDBACK_HISTORY_SIZE];
	double older_logs[CARD_FEEDBACK_HISTORY_SIZE];
	double recent_logs[CARD_FEEDBACK_HISTORY_SIZE];
	double mean = 0.0;
	double sum_squares = 0.0;
	double median_log;
	double variance;
	double maturity;
	double stability;
	double outlier_penalty;
	double drift_penalty = 1.0;
	int outliers = 0;
	int older_count = 0;
	int recent_count = 0;
	int count = entry->history_count;
	int first;
	int i;

	if (count == 0)
	{
		entry->recent_median_rows = 0.0;
		entry->recent_median_q_error = 0.0;
		entry->recent_p90_q_error = 0.0;
		entry->recent_log_stddev = 0.0;
		entry->recent_log_mad = 0.0;
		entry->recent_log_slope = 0.0;
		entry->drift_ratio = 1.0;
		entry->confidence = 0.0;
		entry->recent_outlier_count = 0;
		entry->trend_direction = 0;
		return;
	}

	first = (entry->history_next + CARD_FEEDBACK_HISTORY_SIZE - count) %
		CARD_FEEDBACK_HISTORY_SIZE;
	for (i = 0; i < count; i++)
	{
		int slot = (first + i) % CARD_FEEDBACK_HISTORY_SIZE;
		CardFeedbackObservation *observation = &entry->history[slot];
		double actual = observation->actual_rows;
		double raw = observation->raw_est_rows;
		double lower = Min(raw, actual);
		double upper = Max(raw, actual);
		double log_actual = log(actual + 1.0);

		actual_rows[i] = actual;
		q_errors[i] = (upper + 1.0) / (lower + 1.0);
		log_rows[i] = log_actual;

		// Welford's update avoids subtracting two large second moments.
		{
			double delta = log_actual - mean;

			mean += delta / (i + 1);
			sum_squares += delta * (log_actual - mean);
		}

		if (i < count / 2)
			older_logs[older_count++] = log_actual;
		else
			recent_logs[recent_count++] = log_actual;
	}

	entry->recent_median_rows = CardFeedbackMedian(actual_rows, count);
	entry->recent_median_q_error = CardFeedbackMedian(q_errors, count);
	entry->recent_p90_q_error = CardFeedbackPercentile(q_errors, count, 0.90);
	for (i = 0; i < count; i++)
		deviations[i] = log_rows[i];
	median_log = CardFeedbackMedian(deviations, count);
	for (i = 0; i < count; i++)
		deviations[i] = fabs(log_rows[i] - median_log);
	entry->recent_log_mad = CardFeedbackMedian(deviations, count);

	// Flag isolated jumps without letting them move the center of the sample.
	for (i = 0; i < count; i++)
		if (fabs(log_rows[i] - median_log) > 3.0 * 1.4826 * entry->recent_log_mad)
			outliers++;
	entry->recent_outlier_count = outliers;
	outlier_penalty = 1.0 / (1.0 + (double) outliers / count);
	variance = sum_squares / count;
	if (variance < 0.0)
		variance = 0.0;
	entry->recent_log_stddev = sqrt(variance);
	entry->recent_log_slope = CardFeedbackLogSlope(log_rows, count);
	if (entry->recent_log_slope > CARD_FEEDBACK_TREND_LOG_THRESHOLD)
		entry->trend_direction = 1;
	else if (entry->recent_log_slope < -CARD_FEEDBACK_TREND_LOG_THRESHOLD)
		entry->trend_direction = -1;
	else
		entry->trend_direction = 0;
	entry->drift_ratio = 1.0;

	// Compare the older and newer halves to detect a changing count regime.
	if (count >= 4 && older_count > 0 && recent_count > 0)
	{
		double older_median = CardFeedbackMedian(older_logs, older_count);
		double recent_median = CardFeedbackMedian(recent_logs, recent_count);
		double log_drift = recent_median - older_median;

		if (log_drift > log(DBL_MAX))
			entry->drift_ratio = DBL_MAX;
		else if (log_drift < log(DBL_MIN))
			entry->drift_ratio = 0.0;
		else
			entry->drift_ratio = exp(log_drift);
		drift_penalty = 1.0 / (1.0 + fabs(log_drift));
	}

	// Repeated evidence raises trust; unstable or shifting counts lower it.
	maturity = (double) entry->observations /
		((double) entry->observations + 1.0);
	stability = 1.0 / (1.0 + entry->recent_log_mad);
	entry->confidence = maturity * stability * drift_penalty * outlier_penalty;
}

// Let stale feedback fade even when its retained observations were consistent.
static double
CardFeedbackFreshness(TimestampTz last_updated, TimestampTz now)
{
	const int64 half_life_usec = INT64CONST(604800000000);
	int64 age_usec = now - last_updated;
	double half_lives;

	if (age_usec <= 0)
		return 1.0;
	half_lives = (double) age_usec / (double) half_life_usec;
	return exp(-0.6931471805599453 * half_lives);
}

Size
CardFeedbackShmemSize(void)
{
	return hash_estimate_size(CARD_FEEDBACK_MAX_ENTRIES, sizeof(CardFeedbackEntry));
}

void
CardFeedbackShmemInit(void)
{
	HASHCTL ctl;

	MemSet(&ctl, 0, sizeof(ctl));
	ctl.keysize = sizeof(CardFeedbackKey);
	ctl.entrysize = sizeof(CardFeedbackEntry);
	CardFeedbackHash = ShmemInitHash("Cardinality feedback statistics",
									CARD_FEEDBACK_MAX_ENTRIES,
									CARD_FEEDBACK_MAX_ENTRIES,
									&ctl, HASH_ELEM | HASH_BLOBS | HASH_FIXED_SIZE);
}

void
CardFeedbackRecord(uint64 signature, double raw_rows, double actual_rows,
				   const char *identity)
{
	CardFeedbackKey key;
	CardFeedbackEntry *entry;
#ifdef CARD_FEEDBACK_PROFILE
	bool profile = message_level_is_interesting(DEBUG2);
	instr_time start, acquired, finished, evict_start, evict_end;
	const char *op = "update";
	bool evicted = false;
#endif

	if (signature == 0 || CardFeedbackHash == NULL)
		return;
	MemSet(&key, 0, sizeof(key));
	key.database_id = MyDatabaseId;
	key.user_id = GetUserId();
	key.signature = signature;
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
		// start the clock before asking for the lock, so the wait gets
		// counted too.
		INSTR_TIME_SET_CURRENT(start);
#endif
	LWLockAcquire(CardFeedbackLock, LW_EXCLUSIVE);
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
		// the lock is ours now. save the time so we know how long we waited.
		INSTR_TIME_SET_CURRENT(acquired);
#endif
	entry = hash_search(CardFeedbackHash, &key, HASH_FIND, NULL);
	if (entry == NULL)
	{
#ifdef CARD_FEEDBACK_PROFILE
		op = "insert";
#endif
		if (hash_get_num_entries(CardFeedbackHash) == CARD_FEEDBACK_MAX_ENTRIES)
		{
			HASH_SEQ_STATUS scan;
			CardFeedbackEntry *candidate;
			CardFeedbackEntry *oldest = NULL;

#ifdef CARD_FEEDBACK_PROFILE
			// a full store must remove an old count first. measure that work
			// on its own.
			op = "replace";
			evicted = true;
			if (profile)
				// start timing the search for a count to remove.
				INSTR_TIME_SET_CURRENT(evict_start);
#endif
			// if the store is full, make room by removing the count we
			// updated least recently.
			hash_seq_init(&scan, CardFeedbackHash);
			while ((candidate = hash_seq_search(&scan)) != NULL)
				if (oldest == NULL || candidate->last_updated < oldest->last_updated)
					oldest = candidate;
			hash_search(CardFeedbackHash, &oldest->key, HASH_REMOVE, NULL);
#ifdef CARD_FEEDBACK_PROFILE
			if (profile)
				INSTR_TIME_SET_CURRENT(evict_end);
#endif
		}
		entry = hash_search(CardFeedbackHash, &key, HASH_ENTER_NULL, NULL);
		if (entry != NULL)
		{
			entry->identity[0] = '\0';
			if (identity != NULL)
				strlcpy(entry->identity, identity, sizeof(entry->identity));
			entry->observations = 0;
			entry->ema_actual_rows = actual_rows;
			entry->history_next = 0;
			entry->history_count = 0;
			entry->drift_ratio = 1.0;
			entry->confidence = 0.0;
		}
	}
	/* A hash collision must never merge a reusable identity with another entry. */
	if (entry != NULL &&
		strcmp(entry->identity, identity != NULL ? identity : "") == 0)
	{
		entry->raw_est_rows = raw_rows;
		entry->last_actual_rows = actual_rows;
		if (entry->observations > 0)
			entry->ema_actual_rows = 0.5 * actual_rows + 0.5 * entry->ema_actual_rows;
		entry->observations++;
		entry->last_updated = GetCurrentTimestamp();
		CardFeedbackRememberObservation(entry, raw_rows, actual_rows,
										entry->last_updated);
		CardFeedbackRefreshSummary(entry);
	}
	LWLockRelease(CardFeedbackLock);
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
	{
		// let other connections use the store before we write to the log.
		INSTR_TIME_SET_CURRENT(finished);
		profile_store(op, start, acquired, finished, entry != NULL);
		if (evicted)
			profile_store("evict", evict_start, evict_start, evict_end, true);
	}
#endif
}

CardFeedbackEntry *
CardFeedbackSnapshot(int *count)
{
	CardFeedbackEntry *copy = palloc(sizeof(CardFeedbackEntry) * CARD_FEEDBACK_MAX_ENTRIES);
	HASH_SEQ_STATUS scan;
	CardFeedbackEntry *entry;
	TimestampTz now = GetCurrentTimestamp();

	*count = 0;
	LWLockAcquire(CardFeedbackLock, LW_SHARED);
	hash_seq_init(&scan, CardFeedbackHash);
	while ((entry = hash_seq_search(&scan)) != NULL)
	{
		if (entry->key.database_id == MyDatabaseId && entry->key.user_id == GetUserId())
		{
			copy[*count] = *entry;
			copy[*count].confidence *= CardFeedbackFreshness(entry->last_updated, now);
			(*count)++;
		}
	}
	LWLockRelease(CardFeedbackLock);
	return copy;
}

// Copy observations in chronological order before SQL formats them unlocked.
CardFeedbackHistoryRow *
CardFeedbackHistorySnapshot(int *count)
{
	int maximum = CARD_FEEDBACK_MAX_ENTRIES * CARD_FEEDBACK_HISTORY_SIZE;
	CardFeedbackHistoryRow *copy = palloc(sizeof(CardFeedbackHistoryRow) * maximum);
	HASH_SEQ_STATUS scan;
	CardFeedbackEntry *entry;

	*count = 0;
	LWLockAcquire(CardFeedbackLock, LW_SHARED);
	hash_seq_init(&scan, CardFeedbackHash);
	while ((entry = hash_seq_search(&scan)) != NULL)
	{
		int first;
		int i;

		if (entry->key.database_id != MyDatabaseId || entry->key.user_id != GetUserId())
			continue;
		first = (entry->history_next + CARD_FEEDBACK_HISTORY_SIZE -
				 entry->history_count) % CARD_FEEDBACK_HISTORY_SIZE;
		for (i = 0; i < entry->history_count; i++)
		{
			int slot = (first + i) % CARD_FEEDBACK_HISTORY_SIZE;
			CardFeedbackObservation *observation = &entry->history[slot];
			CardFeedbackHistoryRow *row = &copy[(*count)++];

			row->signature = entry->key.signature;
			row->observation_id = observation->observation_id;
			row->raw_est_rows = observation->raw_est_rows;
			row->actual_rows = observation->actual_rows;
			row->observed_at = observation->observed_at;
		}
	}
	LWLockRelease(CardFeedbackLock);
	return copy;
}

// Remove matching keys after the hash scan has completed under one lock.
static int
CardFeedbackRemoveEntries(bool remove_all, TimestampTz cutoff)
{
	CardFeedbackKey *keys;
	int removed = 0;
	int i;

	keys = palloc(sizeof(CardFeedbackKey) * CARD_FEEDBACK_MAX_ENTRIES);
	LWLockAcquire(CardFeedbackLock, LW_EXCLUSIVE);
	{
		HASH_SEQ_STATUS scan;
		CardFeedbackEntry *entry;

		hash_seq_init(&scan, CardFeedbackHash);
		while ((entry = hash_seq_search(&scan)) != NULL)
		{
			bool in_scope = entry->key.database_id == MyDatabaseId &&
				entry->key.user_id == GetUserId();
			bool expired = entry->last_updated < cutoff;

			if (in_scope && (remove_all || expired))
				keys[removed++] = entry->key;
		}
	}
	for (i = 0; i < removed; i++)
		hash_search(CardFeedbackHash, &keys[i], HASH_REMOVE, NULL);
	LWLockRelease(CardFeedbackLock);
	pfree(keys);
	return removed;
}

int
CardFeedbackReset(void)
{
	// Reset ignores age and clears every entry in the current scope.
	return CardFeedbackRemoveEntries(true, 0);
}

int
CardFeedbackPrune(int64 max_age_usec)
{
	TimestampTz now;
	TimestampTz cutoff;

	if (max_age_usec < 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("feedback age must not be negative")));

	// The SQL wrapper bounds this duration to signed 32-bit seconds.
	now = GetCurrentTimestamp();
	cutoff = now - max_age_usec;
	return CardFeedbackRemoveEntries(false, cutoff);
}

bool
CardFeedbackLookup(uint64 signature, const char *identity, double *rows,
				   double *confidence)
{
	CardFeedbackKey key;
	CardFeedbackEntry *entry;
	bool found = false;
	TimestampTz now = GetCurrentTimestamp();
#ifdef CARD_FEEDBACK_PROFILE
	bool profile = message_level_is_interesting(DEBUG2);
	instr_time start, acquired, finished;
#endif

	if (CardFeedbackHash == NULL || identity == NULL || signature == 0)
		return false;
	MemSet(&key, 0, sizeof(key));
	key.database_id = MyDatabaseId;
	key.user_id = GetUserId();
	key.signature = signature;
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
		// start the clock before asking for the lock, so the wait gets
		// counted too.
		INSTR_TIME_SET_CURRENT(start);
#endif
	LWLockAcquire(CardFeedbackLock, LW_SHARED);
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
		// the lock is ours now. save the time so we know how long we waited.
		INSTR_TIME_SET_CURRENT(acquired);
#endif
	entry = hash_search(CardFeedbackHash, &key, HASH_FIND, NULL);
	if (entry != NULL && entry->observations > 0 &&
		strcmp(entry->identity, identity) == 0)
	{
		*rows = entry->last_actual_rows;
		*confidence = entry->confidence *
			CardFeedbackFreshness(entry->last_updated, now);
		found = true;
	}
	LWLockRelease(CardFeedbackLock);
#ifdef CARD_FEEDBACK_PROFILE
	if (profile)
	{
		// let other connections use the store before we write to the log.
		INSTR_TIME_SET_CURRENT(finished);
		profile_store("lookup", start, acquired, finished, found);
	}
#endif
	return found;
}
