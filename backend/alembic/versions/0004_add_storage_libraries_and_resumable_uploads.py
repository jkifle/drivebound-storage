"""add storage libraries and resumable uploads

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_libraries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), server_default="idle", nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path", name="uq_external_libraries_path"),
    )
    op.create_index("ix_external_libraries_user_id", "external_libraries", ["user_id"])
    op.add_column("assets", sa.Column("storage_source", sa.String(32), server_default="managed", nullable=False))
    op.add_column("assets", sa.Column("external_library_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_assets_external_library_id",
        "assets",
        "external_libraries",
        ["external_library_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_assets_external_library_id", "assets", ["external_library_id"])
    op.create_table(
        "upload_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("total_size", sa.BigInteger(), nullable=False),
        sa.Column("offset", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("expected_checksum", sa.String(64), nullable=True),
        sa.Column("staging_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("staging_path", name="uq_upload_sessions_staging_path"),
    )
    op.create_index("ix_upload_sessions_user_id", "upload_sessions", ["user_id"])
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_upload_sessions_expires_at", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_user_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")
    op.drop_index("ix_assets_external_library_id", table_name="assets")
    op.drop_constraint("fk_assets_external_library_id", "assets", type_="foreignkey")
    op.drop_column("assets", "external_library_id")
    op.drop_column("assets", "storage_source")
    op.drop_index("ix_external_libraries_user_id", table_name="external_libraries")
    op.drop_table("external_libraries")

