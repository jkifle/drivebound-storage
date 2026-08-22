import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountDeletionJob(Base):
    """Durable account-deletion tombstone and broker-independent outbox.

    ``subject_id`` deliberately has no foreign key.  The job must survive the
    final deletion of the User row, and it must remain recoverable while the
    account's ordinary object graph is being removed one item at a time.
    Once cleanup completes, ``subject_id`` is erased; aggregate evidence and
    the non-identifying job id remain.
    """

    __tablename__ = "account_deletion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'queued', 'running', 'retry', 'manual_review', 'completed')",
            name="ck_account_deletion_jobs_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_account_deletion_jobs_attempt_count"),
        CheckConstraint("failure_count >= 0", name="ck_account_deletion_jobs_failure_count"),
        CheckConstraint("assets_purged >= 0", name="ck_account_deletion_jobs_assets_purged"),
        CheckConstraint("uploads_purged >= 0", name="ck_account_deletion_jobs_uploads_purged"),
        CheckConstraint("retained_legacy_paths >= 0", name="ck_account_deletion_jobs_retained_legacy_paths"),
        UniqueConstraint("subject_id", name="uq_account_deletion_jobs_subject_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    assets_purged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    uploads_purged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retained_legacy_paths: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Earliest operator review date.  The independent suppression marker is
    # never pruned automatically; actual archive/off-site inventory governs it.
    suppression_review_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
