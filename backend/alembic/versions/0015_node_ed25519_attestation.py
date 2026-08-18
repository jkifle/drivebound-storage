"""add Ed25519 node attestation and credential rotation state

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing clients generated a random string instead of an Ed25519 key.
    # They are deliberately blocked until explicitly re-paired; migration must
    # never silently bless those stored values as cryptographic identities.
    op.add_column(
        "paired_nodes",
        sa.Column(
            "attestation_state",
            sa.String(32),
            nullable=False,
            server_default="legacy_repair_required",
        ),
    )
    op.add_column("paired_nodes", sa.Column("last_attested_at", sa.DateTime(timezone=True)))
    op.add_column("paired_nodes", sa.Column("last_attestation_timestamp_ms", sa.BigInteger()))
    op.add_column("paired_nodes", sa.Column("secret_rotated_at", sa.DateTime(timezone=True)))
    op.create_index("ix_paired_nodes_attestation_state", "paired_nodes", ["attestation_state"])
    op.create_index(
        "uq_paired_nodes_active_attested_public_key",
        "paired_nodes",
        ["public_key"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL AND attestation_state = 'verified'"),
    )


def downgrade() -> None:
    op.drop_index("uq_paired_nodes_active_attested_public_key", table_name="paired_nodes")
    op.drop_index("ix_paired_nodes_attestation_state", table_name="paired_nodes")
    op.drop_column("paired_nodes", "secret_rotated_at")
    op.drop_column("paired_nodes", "last_attestation_timestamp_ms")
    op.drop_column("paired_nodes", "last_attested_at")
    op.drop_column("paired_nodes", "attestation_state")
