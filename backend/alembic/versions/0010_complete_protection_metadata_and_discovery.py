"""complete protection metadata and discovery

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("file_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("file_modified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("assets", sa.Column("lens_model", sa.String(255), nullable=True))
    op.add_column("assets", sa.Column("orientation", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("assets", sa.Column("restore_status", sa.String(32), server_default="not_requested", nullable=False))
    op.add_column("assets", sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("asset_replicas", sa.Column("metadata_path", sa.Text(), nullable=True))
    op.add_column("upload_sessions", sa.Column("file_created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("upload_sessions", sa.Column("file_modified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_assets_user_file_modified", "assets", ["user_id", "file_modified_at"])


def downgrade() -> None:
    op.drop_index("ix_assets_user_file_modified", table_name="assets")
    op.drop_column("upload_sessions", "file_modified_at")
    op.drop_column("upload_sessions", "file_created_at")
    op.drop_column("asset_replicas", "metadata_path")
    op.drop_column("assets", "restored_at")
    op.drop_column("assets", "restore_status")
    op.drop_column("assets", "metadata_json")
    op.drop_column("assets", "orientation")
    op.drop_column("assets", "lens_model")
    op.drop_column("assets", "file_modified_at")
    op.drop_column("assets", "file_created_at")
