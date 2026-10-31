"""status check constraints

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31

Written by hand: Alembic does not autogenerate CHECK constraints. The value lists
mirror TicketStatus / RunStatus / TestStatus in app/db/models.py. They are repeated
here on purpose: a migration must describe the schema as it was at that point in
time, even if the enums change later.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECKS = {
    ("tickets", "ck_tickets_status"): "status IN ('open', 'running', 'completed', 'needs_human_review')",
    ("agent_runs", "ck_agent_runs_status"): "status IN ('pending', 'running', 'passed', 'failed', 'needs_human_review')",
    ("test_results", "ck_test_results_status"): "status IN ('passed', 'failed', 'error')",
}


def upgrade() -> None:
    for (table, name), condition in CHECKS.items():
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    for table, name in CHECKS:
        op.drop_constraint(name, table, type_="check")
