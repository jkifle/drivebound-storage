import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.external_library import ExternalLibrary
    from app.models.user import User


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_user_checksum", "user_id", "checksum"),
        UniqueConstraint("user_id", "logical_id", "version", name="uq_assets_logical_version"),
        CheckConstraint("version > 0", name="ck_assets_version_positive"),
        CheckConstraint(
            "lifecycle_state IN ('active', 'superseded', 'trashed', 'purging')",
            name="ck_assets_lifecycle_state",
        ),
        CheckConstraint(
            "(lifecycle_state IN ('trashed', 'purging') AND trashed_at IS NOT NULL AND purge_after IS NOT NULL) "
            "OR (lifecycle_state IN ('active', 'superseded') AND purge_after IS NULL)",
            name="ck_assets_retention_state",
        ),
        Index(
            "uq_assets_active_logical",
            "user_id",
            "logical_id",
            unique=True,
            postgresql_where=text("lifecycle_state = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # A logical item has immutable Asset rows for each content revision. New
    # installations assign this at insert time; the Phase 2 migration maps old
    # rows to their own logical item IDs.
    logical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active", index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str | None] = mapped_column(String(128))
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    encryption_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    file_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    camera_make: Mapped[str | None] = mapped_column(String(255))
    camera_model: Mapped[str | None] = mapped_column(String(255))
    lens_model: Mapped[str | None] = mapped_column(String(255))
    orientation: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    semantic_text: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384))
    perceptual_hash: Mapped[str | None] = mapped_column(String(32), index=True)
    intelligence_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    thumbnail_path: Mapped[str | None] = mapped_column(Text)
    preview_path: Mapped[str | None] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    processing_error: Mapped[str | None] = mapped_column(Text)
    storage_source: Mapped[str] = mapped_column(String(32), nullable=False, default="managed", server_default="managed")
    external_library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("external_libraries.id", ondelete="SET NULL"), index=True
    )
    original_filename: Mapped[str | None] = mapped_column(String(1024))
    relative_path: Mapped[str | None] = mapped_column(Text)
    protection_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unprotected", server_default="unprotected"
    )
    restore_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_requested", server_default="not_requested"
    )
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user: Mapped["User"] = relationship(back_populates="assets")
    external_library: Mapped["ExternalLibrary | None"] = relationship(back_populates="assets")
