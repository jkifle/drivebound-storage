"""add per-user media encryption

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing files remain readable as encryption_version=0. New managed media
    # uses version 1 and stores the checksum of its encrypted representation.
    op.add_column("users", sa.Column("media_key_encrypted", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("media_key_version", sa.BigInteger(), server_default="1", nullable=False))
    op.add_column("assets", sa.Column("storage_checksum", sa.String(64), nullable=True))
    op.add_column("assets", sa.Column("encryption_version", sa.Integer(), server_default="0", nullable=False))
    op.execute("UPDATE assets SET storage_checksum = checksum WHERE storage_checksum IS NULL")
    op.alter_column("assets", "storage_checksum", nullable=False)


def downgrade() -> None:
    op.drop_column("assets", "encryption_version")
    op.drop_column("assets", "storage_checksum")
    op.drop_column("users", "media_key_version")
    op.drop_column("users", "media_key_encrypted")
