EXTENSION = card_feedback
MODULE_big = card_feedback
OBJS = card_feedback.o
DATA = card_feedback--1.0.sql
# Use the build rules and install paths from the selected PostgreSQL build.
PG_CONFIG ?= pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
