"""add device push tokens

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("push_token", sa.String(255), nullable=True))
    op.add_column("devices", sa.Column("push_token_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_devices_push_token", "devices", ["push_token"])


def downgrade() -> None:
    op.drop_constraint("uq_devices_push_token", "devices", type_="unique")
    op.drop_column("devices", "push_token_updated_at")
    op.drop_column("devices", "push_token")
