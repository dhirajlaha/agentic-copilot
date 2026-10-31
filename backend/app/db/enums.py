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
    FAILED = "failed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"  # retry cap (2) exhausted


class TestStatus(enum.StrEnum):
    __test__ = False  # stop pytest trying to collect this as a test class
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
