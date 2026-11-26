"""add site_id to approval_nodes

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-11-26 12:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5f6a7b8c9d0'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('approval_nodes') as batch:
        batch.add_column(sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=True))
        batch.create_index('ix_approval_nodes_site_id', ['site_id'])

    # Safely backfill any existing unassigned nodes to the first available site dynamically
    conn = op.get_bind()
    first_site = conn.execute(sa.text("SELECT id FROM sites ORDER BY id ASC LIMIT 1")).scalar()
    if first_site:
        conn.execute(
            sa.text("UPDATE approval_nodes SET site_id = :sid WHERE site_id IS NULL"),
            {"sid": first_site}
        )


def downgrade():
    with op.batch_alter_table('approval_nodes') as batch:
        batch.drop_index('ix_approval_nodes_site_id')
        batch.drop_column('site_id')
