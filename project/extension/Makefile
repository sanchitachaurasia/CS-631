EXTENSION = card_feedback
MODULE_big = card_feedback
OBJS = card_feedback.o
DATA = card_feedback--1.0.sql
PG_CONFIG ?= pg_config
# use pg config to build the extension for the chosen postgresql.
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
