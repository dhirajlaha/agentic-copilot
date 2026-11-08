"""Allowed values for status columns. The database enforces them with CHECK constraints
(not native ENUMs: adding or removing a value is far simpler that way)."""

import enum


class TicketStatus(enum.StrEnum):
    OPEN = "open"
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class RunStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"  # the pipeline itself errored (see agent_runs.error)
    NEEDS_HUMAN_REVIEW = "needs_human_review"  # retry cap (2) exhausted
    # Pipeline finished but had nothing to verify (no test step yet). Once the test
    # executor exists, runs end as passed or needs_human_review instead.
    COMPLETED = "completed"


ACTIVE_RUN_STATUSES = (RunStatus.PENDING, RunStatus.RUNNING)


class TestStatus(enum.StrEnum):
    __test__ = False  # stop pytest trying to collect this as a test class
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class IndexStatus(enum.StrEnum):
    """State of a repository's code index (`repo_connections.index_status`)."""

    NOT_INDEXED = "not_indexed"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"  # the last attempt failed; a retry resumes from what was saved
