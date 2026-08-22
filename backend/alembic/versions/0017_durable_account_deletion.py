"""add durable account-deletion jobs

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Inviter attribution is not ownership. Deleting an inviter must not remove
    # another user's already-joined membership in a third party's album.
    op.drop_constraint("album_members_invited_by_fkey", "album_members", type_="foreignkey")
    op.alter_column("album_members", "invited_by", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.create_foreign_key(
        "album_members_invited_by_fkey",
        "album_members",
        "users",
        ["invited_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "account_deletion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # No User foreign key: cleanup evidence/outbox state must survive the
        # final account-row deletion.  This value is nulled on completion.
        sa.Column("subject_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("assets_purged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploads_purged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retained_legacy_paths", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("suppression_review_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'retry', 'manual_review', 'completed')",
            name="ck_account_deletion_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_account_deletion_jobs_attempt_count"),
        sa.CheckConstraint("failure_count >= 0", name="ck_account_deletion_jobs_failure_count"),
        sa.CheckConstraint("assets_purged >= 0", name="ck_account_deletion_jobs_assets_purged"),
        sa.CheckConstraint("uploads_purged >= 0", name="ck_account_deletion_jobs_uploads_purged"),
        sa.CheckConstraint("retained_legacy_paths >= 0", name="ck_account_deletion_jobs_retained_legacy_paths"),
        sa.UniqueConstraint("subject_id", name="uq_account_deletion_jobs_subject_id"),
    )
    op.create_index("ix_account_deletion_jobs_status", "account_deletion_jobs", ["status"])
    op.create_index("ix_account_deletion_jobs_next_attempt_at", "account_deletion_jobs", ["next_attempt_at"])
    op.create_index("ix_account_deletion_jobs_lease_expires_at", "account_deletion_jobs", ["lease_expires_at"])
    op.create_index("ix_account_deletion_jobs_suppression_review_after", "account_deletion_jobs", ["suppression_review_after"])
    op.create_index("ix_account_deletion_jobs_completed_at", "account_deletion_jobs", ["completed_at"])


def downgrade() -> None:
    # Losing even a completed row can discard restore-suppression evidence or
    # strand a crypto-erased account.  Operators must archive/reconcile the
    # independent suppression ledger and explicitly remove every row before a
    # code/schema rollback.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM account_deletion_jobs) THEN
                RAISE EXCEPTION 'cannot downgrade 0017 while account deletion evidence exists';
            END IF;
        END
        $$;
        """
    )
    op.drop_table("account_deletion_jobs")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM album_members WHERE invited_by IS NULL) THEN
                RAISE EXCEPTION 'cannot restore inviter CASCADE while preserved memberships lack attribution';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint("album_members_invited_by_fkey", "album_members", type_="foreignkey")
    op.alter_column("album_members", "invited_by", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_foreign_key(
        "album_members_invited_by_fkey",
        "album_members",
        "users",
        ["invited_by"],
        ["id"],
        ondelete="CASCADE",
    )
