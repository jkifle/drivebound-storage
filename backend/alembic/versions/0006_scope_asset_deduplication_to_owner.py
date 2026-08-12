"""scope asset deduplication to owner

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_assets_checksum", "assets", type_="unique")
    op.create_unique_constraint("uq_assets_user_checksum", "assets", ["user_id", "checksum"])


def downgrade() -> None:
    op.drop_constraint("uq_assets_user_checksum", "assets", type_="unique")
    op.create_unique_constraint("uq_assets_checksum", "assets", ["checksum"])
