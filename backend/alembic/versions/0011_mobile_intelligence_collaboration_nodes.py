"""mobile intelligence collaboration nodes

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("ocr_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("semantic_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("embedding", Vector(384), nullable=True))
    op.add_column("assets", sa.Column("intelligence_status", sa.String(32), server_default="pending", nullable=False))
    op.add_column("assets", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_assets_user_intelligence", "assets", ["user_id", "intelligence_status"])
    op.execute("CREATE INDEX ix_assets_embedding_hnsw ON assets USING hnsw (embedding vector_cosine_ops)")

    op.add_column("devices", sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("files_backed_up", sa.Integer(), server_default="0", nullable=False))
    op.add_column("devices", sa.Column("bytes_backed_up", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("upload_sessions", sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_upload_sessions_device_id", "upload_sessions", "devices", ["device_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_upload_sessions_device_id", "upload_sessions", ["device_id"])

    op.create_table(
        "album_members",
        sa.Column("album_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), server_default="viewer", nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["album_id"], ["albums.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("album_id", "user_id"),
    )
    op.create_index("ix_album_members_user_id", "album_members", ["user_id"])
    op.create_table(
        "album_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("album_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(16), server_default="viewer", nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["album_id"], ["albums.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_album_invites_token_hash"),
    )
    op.create_index("ix_album_invites_album_id", "album_invites", ["album_id"])
    op.create_index("ix_album_invites_email", "album_invites", ["email"])

    op.create_table(
        "monitoring_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), server_default="warning", nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_monitoring_events_user_id", "monitoring_events", ["user_id"])
    op.create_index("ix_monitoring_events_asset_id", "monitoring_events", ["asset_id"])
    op.create_index("ix_monitoring_events_kind", "monitoring_events", ["kind"])

    op.create_table(
        "node_pairing_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index("ix_node_pairing_codes_user_id", "node_pairing_codes", ["user_id"])
    op.create_index("ix_node_pairing_codes_expires_at", "node_pairing_codes", ["expires_at"])
    op.create_table(
        "paired_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="offline", nullable=False),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_hash"),
    )
    op.create_index("ix_paired_nodes_user_id", "paired_nodes", ["user_id"])
    op.create_index("ix_paired_nodes_last_seen_at", "paired_nodes", ["last_seen_at"])


def downgrade() -> None:
    op.drop_table("paired_nodes")
    op.drop_table("node_pairing_codes")
    op.drop_table("monitoring_events")
    op.drop_table("album_invites")
    op.drop_table("album_members")
    op.drop_index("ix_upload_sessions_device_id", table_name="upload_sessions")
    op.drop_constraint("fk_upload_sessions_device_id", "upload_sessions", type_="foreignkey")
    op.drop_column("upload_sessions", "device_id")
    op.drop_column("devices", "bytes_backed_up")
    op.drop_column("devices", "files_backed_up")
    op.drop_column("devices", "last_backup_at")
    op.drop_index("ix_assets_embedding_hnsw", table_name="assets")
    op.drop_index("ix_assets_user_intelligence", table_name="assets")
    op.drop_column("assets", "indexed_at")
    op.drop_column("assets", "intelligence_status")
    op.drop_column("assets", "embedding")
    op.drop_column("assets", "semantic_text")
    op.drop_column("assets", "ocr_text")
