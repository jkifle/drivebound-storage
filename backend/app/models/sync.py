import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SyncRoot(Base):
    __tablename__ = "sync_roots"
    __table_args__ = (UniqueConstraint("user_id", "client_id", name="uq_sync_roots_user_client"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", server_default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SyncItem(Base):
    __tablename__ = "sync_items"
    __table_args__ = (UniqueConstraint("root_id", "logical_id", name="uq_sync_items_root_logical"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_roots.id", ondelete="CASCADE"), index=True)
    logical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str] = mapped_column(String(128), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SyncOperation(Base):
    __tablename__ = "sync_operations"
    __table_args__ = (UniqueConstraint("root_id", "client_id", "client_sequence", name="uq_sync_operations_client_sequence"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_roots.id", ondelete="CASCADE"), index=True)
    cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    logical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    base_revision: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="applied", server_default="applied")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    root_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_roots.id", ondelete="CASCADE"), index=True)
    logical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    local_operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_operations.id", ondelete="CASCADE"))
    conflicting_operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sync_operations.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", server_default="open")
    resolution: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
