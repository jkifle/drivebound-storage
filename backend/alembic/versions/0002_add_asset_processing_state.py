"""add asset processing state

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("processing_status", sa.String(32), server_default="pending", nullable=False),
    )
    op.add_column("assets", sa.Column("processing_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "processing_error")
    op.drop_column("assets", "processing_status")

