/* Diagnostic signatures and verified snapshot-scoped cardinality feedback. */
#include "postgres.h"

#include "common/hashfn.h"
#include "access/xact.h"
#include "catalog/pg_class.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_operator_d.h"
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
#include "parser/parsetree.h"
#include "storage/lwlock.h"
#include "storage/shmem.h"
#include "utils/hsearch.h"
#include "utils/timestamp.h"

bool enable_card_feedback_apply = false;
int card_feedback_cached_plan_depth = 0;

/* Validate and normalize a COPY: only built-in int4 equality/boolean filters. */
static bool
CardFeedbackNormalizeExpr(Node *node, void *context)
{
	Bitmapset *allowed = (Bitmapset *) context;

	if (node == NULL)
		return false;
	switch (nodeTag(node))
	{
		case T_Var:
		{
			Var *var = (Var *) node;

			if (!bms_is_member(var->varno, allowed) || var->varlevelsup != 0 ||
				var->varattno <= 0 || var->vartype != INT4OID)
				return true;
			var->location = -1;
			return false;
		}
		case T_Const:
		{
			Const *value = (Const *) node;

			if (value->consttype != INT4OID && value->consttype != BOOLOID)
				return true;
			value->location = -1;
			return false;
		}
		case T_OpExpr:
		{
			OpExpr *op = (OpExpr *) node;

			if (op->opno != Int4EqualOperator)
				return true;
			op->location = -1;
			op->opfuncid = F_INT4EQ;
			break;
		}
		case T_BoolExpr:
			((BoolExpr *) node)->location = -1;
			break;
		case T_RangeTblRef:
			return !bms_is_member(((RangeTblRef *) node)->rtindex, allowed);
		case T_FromExpr:
		{
			FromExpr *from = (FromExpr *) node;
			ListCell *lc;

			foreach(lc, from->fromlist)
				if (CardFeedbackNormalizeExpr(lfirst(lc), allowed))
					return true;
			return CardFeedbackNormalizeExpr(from->quals, allowed);
		}
		case T_JoinExpr:
		{
			JoinExpr *join = (JoinExpr *) node;

			return join->jointype != JOIN_INNER ||
				CardFeedbackNormalizeExpr(join->larg, allowed) ||
				CardFeedbackNormalizeExpr(join->rarg, allowed) ||
				CardFeedbackNormalizeExpr(join->quals, allowed);
		}
		case T_List:
			break;
		default:
			return true;
	}
	return expression_tree_walker(node, CardFeedbackNormalizeExpr, context);
}

/* Freeze the whole supported query context before join search changes paths. */
static char *
CardFeedbackReadonlyContext(PlannerInfo *root)
{
	Query *query = root->parse;
	StringInfoData key;
	ListCell *lc;
	Bitmapset *allowed = NULL;
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
		query->commandType != CMD_SELECT ||
		query->hasAggs || query->hasWindowFuncs || query->hasTargetSRFs ||
		query->hasSubLinks || query->hasModifyingCTE || query->hasRowSecurity ||
		query->cteList != NIL || query->groupClause != NIL ||
		query->groupingSets != NIL || query->havingQual != NULL ||
		query->windowClause != NIL || query->distinctClause != NIL ||
		query->sortClause != NIL || query->limitCount != NULL ||
		query->limitOffset != NULL || query->rowMarks != NIL ||
		query->setOperations != NULL)
		return NULL;
	initStringInfo(&key);
	appendStringInfo(&key, "readonly-v2:%u:%u:%d:%lld:%u:",
					 MyDatabaseId, GetUserId(), MyProcPid,
					 (long long) MyStartTimestamp, MyProc->lxid);
	foreach(lc, query->rtable)
	{
		RangeTblEntry *rte = lfirst_node(RangeTblEntry, lc);

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
		allowed = bms_add_member(allowed, rti);
		appendStringInfo(&key, "table:%d:%u;", rti, rte->relid);
	}
	if (bms_num_members(allowed) < 1 || bms_num_members(allowed) > 3)
	{
		pfree(key.data);
		return NULL;
	}
	foreach(lc, query->targetList)
	{
		TargetEntry *target = lfirst_node(TargetEntry, lc);
		Node *expr = (Node *) copyObject(target->expr);

		if (target->resjunk || !IsA(expr, Var) ||
			CardFeedbackNormalizeExpr(expr, allowed))
		{
			pfree(key.data);
			return NULL;
		}
		serialized = nodeToString(expr);
		appendStringInfo(&key, "target:%s;", serialized);
		pfree(serialized);
	}
	jointree = (Node *) copyObject(query->jointree);
	if (CardFeedbackNormalizeExpr(jointree, allowed))
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

char *
CardFeedbackReadonlyIdentity(PlannerInfo *root, RelOptInfo *rel)
{
	const char *context = CardFeedbackReadonlyContext(root);
	StringInfoData key;
	int member = -1;

	if (context == NULL || bms_is_empty(rel->relids) ||
		!bms_is_subset(rel->relids, root->feedback_reuse_relids) ||
		(rel->reloptkind != RELOPT_BASEREL && rel->reloptkind != RELOPT_JOINREL))
		return NULL;
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

/* Shared observations have server lifetime; reusable identities are transaction-scoped. */
static HTAB *CardFeedbackHash = NULL;

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
	bool found;

	if (signature == 0 || CardFeedbackHash == NULL)
		return;
	MemSet(&key, 0, sizeof(key));
	key.database_id = MyDatabaseId;
	key.user_id = GetUserId();
	key.signature = signature;
	LWLockAcquire(CardFeedbackLock, LW_EXCLUSIVE);
	entry = hash_search(CardFeedbackHash, &key, HASH_FIND, NULL);
	if (entry == NULL && hash_get_num_entries(CardFeedbackHash) < CARD_FEEDBACK_MAX_ENTRIES)
	{
		entry = hash_search(CardFeedbackHash, &key, HASH_ENTER_NULL, &found);
		if (entry != NULL)
		{
			entry->identity[0] = '\0';
			if (identity != NULL)
				strlcpy(entry->identity, identity, sizeof(entry->identity));
			entry->observations = 0;
			entry->ema_actual_rows = actual_rows;
		}
	}
	/* At capacity, skip new identities; existing entries can still be updated. */
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
	}
	LWLockRelease(CardFeedbackLock);
}

CardFeedbackEntry *
CardFeedbackSnapshot(int *count)
{
	CardFeedbackEntry *copy = palloc(sizeof(CardFeedbackEntry) * CARD_FEEDBACK_MAX_ENTRIES);
	HASH_SEQ_STATUS scan;
	CardFeedbackEntry *entry;

	*count = 0;
	LWLockAcquire(CardFeedbackLock, LW_SHARED);
	hash_seq_init(&scan, CardFeedbackHash);
	while ((entry = hash_seq_search(&scan)) != NULL)
	{
		if (entry->key.database_id == MyDatabaseId && entry->key.user_id == GetUserId())
			copy[(*count)++] = *entry;
	}
	LWLockRelease(CardFeedbackLock);
	return copy;
}

int
CardFeedbackReset(void)
{
	CardFeedbackEntry *entries;
	int count;
	int removed = 0;
	int i;

	/* One exclusive critical section makes reset atomic relative to writers. */
	entries = palloc(sizeof(CardFeedbackEntry) * CARD_FEEDBACK_MAX_ENTRIES);
	LWLockAcquire(CardFeedbackLock, LW_EXCLUSIVE);
	{
		HASH_SEQ_STATUS scan;
		CardFeedbackEntry *entry;

		hash_seq_init(&scan, CardFeedbackHash);
		while ((entry = hash_seq_search(&scan)) != NULL)
			if (entry->key.database_id == MyDatabaseId && entry->key.user_id == GetUserId())
				entries[removed++] = *entry;
	}
	count = removed;
	for (i = 0; i < count; i++)
		hash_search(CardFeedbackHash, &entries[i].key, HASH_REMOVE, NULL);
	LWLockRelease(CardFeedbackLock);
	pfree(entries);
	return removed;
}

bool
CardFeedbackLookup(uint64 signature, const char *identity, double *rows)
{
	CardFeedbackKey key;
	CardFeedbackEntry *entry;
	bool found = false;

	if (CardFeedbackHash == NULL || identity == NULL || signature == 0)
		return false;
	MemSet(&key, 0, sizeof(key));
	key.database_id = MyDatabaseId;
	key.user_id = GetUserId();
	key.signature = signature;
	LWLockAcquire(CardFeedbackLock, LW_SHARED);
	entry = hash_search(CardFeedbackHash, &key, HASH_FIND, NULL);
	if (entry != NULL && entry->observations > 0 &&
		strcmp(entry->identity, identity) == 0)
	{
		*rows = entry->last_actual_rows;
		found = true;
	}
	LWLockRelease(CardFeedbackLock);
	return found;
}
