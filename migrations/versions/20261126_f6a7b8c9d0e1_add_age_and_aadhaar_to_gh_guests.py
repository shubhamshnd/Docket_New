"""add age and aadhaar_card to gh_guests

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-26 14:35:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('gh_guests') as batch:
        batch.add_column(sa.Column('age', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('aadhaar_card', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('gh_guests') as batch:
        batch.drop_column('aadhaar_card')
        batch.drop_column('age')
