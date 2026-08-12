import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.external_library import ExternalLibrary
    from app.models.user import User


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("user_id", "checksum", name="uq_assets_user_checksum"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
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
