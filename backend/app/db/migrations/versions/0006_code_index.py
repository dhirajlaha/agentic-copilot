"""code index: chunk metadata, 768-dim embeddings, HNSW index, index state on repos

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy.vector
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_STATUSES = "index_status IN ('not_indexed', 'indexing', 'ready', 'failed')"


def upgrade() -> None:
    # Nothing could write chunks before the indexer existed, and vectors of a different
    # size cannot be converted, so refuse rather than silently dropping data.
    existing = op.get_bind().scalar(sa.text("SELECT count(*) FROM code_chunks"))
    if existing:
        raise RuntimeError(
            f"{existing} code_chunks row(s) exist; they use the old 384-dim embeddings "
            "and must be deleted before migrating (they will be re-created by indexing)."
        )

    op.drop_column('code_chunks', 'embedding')
    op.add_column('code_chunks', sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=768), nullable=False))
    op.add_column('code_chunks', sa.Column('blob_sha', sa.String(length=40), nullable=False))
    op.add_column('code_chunks', sa.Column('chunk_index', sa.Integer(), nullable=False))
    op.add_column('code_chunks', sa.Column('start_line', sa.Integer(), nullable=False))
    op.add_column('code_chunks', sa.Column('end_line', sa.Integer(), nullable=False))
    op.create_unique_constraint(
        'uq_code_chunks_repo_connection_id_file_path_chunk_index', 'code_chunks',
        ['repo_connection_id', 'file_path', 'chunk_index'],
    )
    op.create_index('ix_code_chunks_repo_file', 'code_chunks', ['repo_connection_id', 'file_path'])
    op.create_index(
        'ix_code_chunks_embedding_hnsw', 'code_chunks', ['embedding'],
        postgresql_using='hnsw', postgresql_ops={'embedding': 'vector_cosine_ops'},
    )

    op.add_column('repo_connections', sa.Column('index_status', sa.String(length=32), server_default=sa.text("'not_indexed'"), nullable=False))
    op.add_column('repo_connections', sa.Column('index_lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('repo_connections', sa.Column('indexed_commit_sha', sa.String(length=40), nullable=True))
    op.add_column('repo_connections', sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('repo_connections', sa.Column('index_error', sa.Text(), nullable=True))
    op.create_check_constraint('ck_repo_connections_index_status', 'repo_connections', INDEX_STATUSES)


def downgrade() -> None:
    op.drop_constraint('ck_repo_connections_index_status', 'repo_connections', type_='check')
    for column in ('index_error', 'indexed_at', 'indexed_commit_sha', 'index_lease_expires_at', 'index_status'):
        op.drop_column('repo_connections', column)
    op.drop_index('ix_code_chunks_embedding_hnsw', table_name='code_chunks')
    op.drop_index('ix_code_chunks_repo_file', table_name='code_chunks')
    op.drop_constraint('uq_code_chunks_repo_connection_id_file_path_chunk_index', 'code_chunks', type_='unique')
    for column in ('end_line', 'start_line', 'chunk_index', 'blob_sha'):
        op.drop_column('code_chunks', column)
    op.drop_column('code_chunks', 'embedding')
    op.add_column('code_chunks', sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False))
