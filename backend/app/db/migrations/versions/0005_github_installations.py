"""github installations: store the installation id instead of an encrypted token

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Before the repo-connect flow existed nothing could create a repo connection, so this
    # table is expected to be empty. If it is not, fail loudly instead of guessing which
    # installation old rows belonged to.
    existing = op.get_bind().scalar(sa.text("SELECT count(*) FROM repo_connections"))
    if existing:
        raise RuntimeError(
            f"{existing} repo_connections row(s) exist without an installation; "
            "delete them (and their tickets) or backfill before migrating."
        )

    op.create_table(
        'github_installations',
        sa.Column('id', postgresql.UUID(as_uuid=True), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('installation_id', sa.BigInteger(), nullable=False),
        sa.Column('account_login', sa.String(length=255), nullable=False),
        sa.Column('account_type', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'installation_id'),
    )
    op.create_index('ix_github_installations_user_id', 'github_installations', ['user_id'])

    op.drop_column('repo_connections', 'installation_token')
    op.add_column('repo_connections', sa.Column('github_installation_id', postgresql.UUID(as_uuid=True), nullable=False))
    op.create_index('ix_repo_connections_github_installation_id', 'repo_connections', ['github_installation_id'])
    op.create_foreign_key(
        'fk_repo_connections_github_installation_id', 'repo_connections', 'github_installations',
        ['github_installation_id'], ['id'], ondelete='RESTRICT',
    )


def downgrade() -> None:
    op.drop_constraint('fk_repo_connections_github_installation_id', 'repo_connections', type_='foreignkey')
    op.drop_index('ix_repo_connections_github_installation_id', table_name='repo_connections')
    op.drop_column('repo_connections', 'github_installation_id')
    op.add_column('repo_connections', sa.Column('installation_token', sa.LargeBinary(), nullable=True))
    op.drop_index('ix_github_installations_user_id', table_name='github_installations')
    op.drop_table('github_installations')
