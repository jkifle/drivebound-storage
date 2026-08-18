"""add storage lifecycle, replication policy, sync journals, and media groups

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_monitoring_account_verification_open",
        "monitoring_events",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'account_storage_verification' AND status = 'open'"),
    )
    op.create_table(
        "storage_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("desired_replica_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("balance_threshold_percent", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("backup_retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_storage_policies_user_id", "storage_policies", ["user_id"])
    op.create_table(
        "storage_drives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "path", name="uq_storage_drives_user_path"),
    )
    op.create_index("ix_storage_drives_user_id", "storage_drives", ["user_id"])

    op.drop_constraint("uq_assets_user_checksum", "assets", type_="unique")
    op.add_column("assets", sa.Column("logical_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE assets SET logical_id = id WHERE logical_id IS NULL")
    op.alter_column("assets", "logical_id", nullable=False)
    op.add_column("assets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("assets", sa.Column("lifecycle_state", sa.String(32), nullable=False, server_default="active"))
    op.add_column("assets", sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.add_column("assets", sa.Column("trashed_at", sa.DateTime(timezone=True)))
    op.add_column("assets", sa.Column("purge_after", sa.DateTime(timezone=True)))
    op.add_column("assets", sa.Column("deleted_by", sa.String(128)))
    op.add_column("assets", sa.Column("perceptual_hash", sa.String(32)))
    op.create_index("ix_assets_user_checksum", "assets", ["user_id", "checksum"])
    op.create_index("ix_assets_logical_id", "assets", ["logical_id"])
    op.create_index("ix_assets_lifecycle_state", "assets", ["lifecycle_state"])
    op.create_index("ix_assets_purge_after", "assets", ["purge_after"])
    op.create_index("ix_assets_perceptual_hash", "assets", ["perceptual_hash"])
    op.create_unique_constraint("uq_assets_logical_version", "assets", ["user_id", "logical_id", "version"])
    op.create_check_constraint("ck_assets_version_positive", "assets", "version > 0")
    op.create_check_constraint(
        "ck_assets_lifecycle_state",
        "assets",
        "lifecycle_state IN ('active', 'superseded', 'trashed', 'purging')",
    )
    op.create_check_constraint(
        "ck_assets_retention_state",
        "assets",
        "(lifecycle_state IN ('trashed', 'purging') AND trashed_at IS NOT NULL AND purge_after IS NOT NULL) "
        "OR (lifecycle_state IN ('active', 'superseded') AND purge_after IS NULL)",
    )
    op.create_index(
        "uq_assets_active_logical",
        "assets",
        ["user_id", "logical_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_state = 'active'"),
    )

    op.add_column("asset_replicas", sa.Column("drive_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("storage_drives.id", ondelete="SET NULL")))
    op.add_column("asset_replicas", sa.Column("verification_status", sa.String(32), nullable=False, server_default="verified"))
    op.add_column("asset_replicas", sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("asset_replicas", sa.Column("last_error", sa.Text()))
    # Immutable revisions may share checksum-addressed replica bytes. The
    # association is unique per asset/drive, not per physical object path.
    op.drop_constraint("uq_asset_replicas_path", "asset_replicas", type_="unique")
    op.create_index("ix_asset_replicas_path", "asset_replicas", ["path"])
    op.create_index("ix_asset_replicas_drive_id", "asset_replicas", ["drive_id"])
    op.create_unique_constraint("uq_asset_replicas_asset_drive", "asset_replicas", ["asset_id", "drive_id"])
    op.create_index(
        "uq_asset_replicas_asset_default_drive",
        "asset_replicas",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("drive_id IS NULL"),
    )

    op.create_table(
        "backup_archives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("verification_detail", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_backup_archives_kind", "backup_archives", ["kind"])

    op.create_table(
        "sync_roots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "client_id", name="uq_sync_roots_user_client"),
    )
    op.create_index("ix_sync_roots_user_id", "sync_roots", ["user_id"])
    op.create_table(
        "sync_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("root_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_roots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logical_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("revision", sa.String(128), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("root_id", "logical_id", name="uq_sync_items_root_logical"),
    )
    op.create_index("ix_sync_items_root_id", "sync_items", ["root_id"])
    op.create_index("ix_sync_items_logical_id", "sync_items", ["logical_id"])
    op.create_index("ix_sync_items_asset_id", "sync_items", ["asset_id"])
    op.create_table(
        "sync_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("root_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_roots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cursor", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=False),
        sa.Column("client_sequence", sa.BigInteger(), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("logical_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_revision", sa.String(128)),
        sa.Column("revision", sa.String(128)),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(32), nullable=False, server_default="applied"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("root_id", "client_id", "client_sequence", name="uq_sync_operations_client_sequence"),
    )
    op.create_index("ix_sync_operations_root_id", "sync_operations", ["root_id"])
    op.create_index("ix_sync_operations_cursor", "sync_operations", ["cursor"])
    op.create_index("ix_sync_operations_logical_id", "sync_operations", ["logical_id"])
    op.create_table(
        "sync_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("root_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_roots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("logical_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_operations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conflicting_operation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_operations.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("resolution", sa.String(32)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_sync_conflicts_root_id", "sync_conflicts", ["root_id"])
    op.create_index("ix_sync_conflicts_logical_id", "sync_conflicts", ["logical_id"])

    op.create_table(
        "media_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("group_key", sa.String(255), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "kind", "group_key", name="uq_media_groups_key"),
    )
    op.create_index("ix_media_groups_user_id", "media_groups", ["user_id"])
    op.create_index("ix_media_groups_kind", "media_groups", ["kind"])
    op.create_table(
        "media_group_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("media_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("similarity", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("group_id", "asset_id", name="uq_media_group_member"),
    )
    op.create_index("ix_media_group_members_group_id", "media_group_members", ["group_id"])
    op.create_index("ix_media_group_members_asset_id", "media_group_members", ["asset_id"])


def downgrade() -> None:
    op.drop_table("media_group_members")
    op.drop_table("media_groups")
    op.drop_table("sync_conflicts")
    op.drop_table("sync_operations")
    op.drop_table("sync_items")
    op.drop_table("sync_roots")
    op.drop_table("backup_archives")
    op.drop_index("uq_asset_replicas_asset_default_drive", table_name="asset_replicas")
    op.drop_constraint("uq_asset_replicas_asset_drive", "asset_replicas", type_="unique")
    op.drop_column("asset_replicas", "last_error")
    op.drop_column("asset_replicas", "failure_count")
    op.drop_column("asset_replicas", "verification_status")
    op.drop_column("asset_replicas", "drive_id")
    op.drop_index("ix_asset_replicas_path", table_name="asset_replicas")
    op.create_unique_constraint("uq_asset_replicas_path", "asset_replicas", ["path"])
    op.drop_index("uq_assets_active_logical", table_name="assets")
    op.drop_constraint("ck_assets_retention_state", "assets", type_="check")
    op.drop_constraint("ck_assets_lifecycle_state", "assets", type_="check")
    op.drop_constraint("ck_assets_version_positive", "assets", type_="check")
    op.drop_constraint("uq_assets_logical_version", "assets", type_="unique")
    op.drop_index("ix_assets_perceptual_hash", table_name="assets")
    op.drop_index("ix_assets_purge_after", table_name="assets")
    op.drop_index("ix_assets_lifecycle_state", table_name="assets")
    op.drop_index("ix_assets_logical_id", table_name="assets")
    op.drop_index("ix_assets_user_checksum", table_name="assets")
    op.drop_column("assets", "perceptual_hash")
    op.drop_column("assets", "deleted_by")
    op.drop_column("assets", "purge_after")
    op.drop_column("assets", "trashed_at")
    op.drop_column("assets", "superseded_at")
    op.drop_column("assets", "lifecycle_state")
    op.drop_column("assets", "version")
    op.drop_column("assets", "logical_id")
    op.create_unique_constraint("uq_assets_user_checksum", "assets", ["user_id", "checksum"])
    op.drop_table("storage_drives")
    op.drop_table("storage_policies")
    op.drop_index("uq_monitoring_account_verification_open", table_name="monitoring_events")
