"""run publication: branch and draft pull request created from a run's patch

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-08 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUBLICATION_STATUSES = (
    "publication_status IS NULL OR publication_status IN ('publishing', 'published', 'failed')"
)


def upgrade() -> None:
    op.add_column('agent_runs', sa.Column('publication_status', sa.String(length=32), nullable=True))
    op.add_column('agent_runs', sa.Column('branch_name', sa.Text(), nullable=True))
    op.add_column('agent_runs', sa.Column('pull_request_number', sa.Integer(), nullable=True))
    op.add_column('agent_runs', sa.Column('pull_request_url', sa.Text(), nullable=True))
    op.add_column('agent_runs', sa.Column('publish_started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_runs', sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('agent_runs', sa.Column('publish_error', sa.Text(), nullable=True))
    op.create_check_constraint('ck_agent_runs_publication_status', 'agent_runs', PUBLICATION_STATUSES)


def downgrade() -> None:
    op.drop_constraint('ck_agent_runs_publication_status', 'agent_runs', type_='check')
    for column in ('publish_error', 'published_at', 'publish_started_at', 'pull_request_url', 'pull_request_number', 'branch_name', 'publication_status'):
        op.drop_column('agent_runs', column)
