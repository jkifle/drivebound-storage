"""add timeline pagination index

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_assets_user_timeline "
        "ON assets (user_id, (COALESCE(taken_at, created_at)) DESC, id DESC)"
    )


def downgrade() -> None:
    op.drop_index("ix_assets_user_timeline", table_name="assets")

