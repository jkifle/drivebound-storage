"""harden node http security

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paired_nodes", sa.Column("node_secret", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_paired_nodes_node_secret", "paired_nodes", ["node_secret"])


def downgrade() -> None:
    op.drop_constraint("uq_paired_nodes_node_secret", "paired_nodes", type_="unique")
    op.drop_column("paired_nodes", "node_secret")
