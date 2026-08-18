import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.asset import Asset


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "(pending_totp_secret_encrypted IS NULL AND pending_totp_expires_at IS NULL "
            "AND pending_totp_session_id IS NULL) OR "
            "(pending_totp_secret_encrypted IS NOT NULL AND pending_totp_expires_at IS NOT NULL "
            "AND pending_totp_session_id IS NOT NULL)",
            name="ck_users_pending_totp_complete",
        ),
        CheckConstraint(
            "(totp_secret_encrypted IS NULL AND totp_enabled_at IS NULL) OR "
            "(totp_secret_encrypted IS NOT NULL AND totp_enabled_at IS NOT NULL)",
            name="ck_users_totp_complete",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    password_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    display_name: Mapped[str | None] = mapped_column(String(120))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    pending_totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    pending_totp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_totp_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    media_key_encrypted: Mapped[str | None] = mapped_column(Text)
    media_key_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    assets: Mapped[list["Asset"]] = relationship(back_populates="user", cascade="all, delete-orphan")
