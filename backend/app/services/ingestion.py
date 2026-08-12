import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.user import User
from app.services.encryption import generate_user_media_key, unwrap_user_media_key, wrap_user_media_key
from app.services.storage import commit_encrypted_original, commit_original, original_path_for, sha256_file


def user_media_key(user: User) -> bytes:
    """Create an account DEK lazily, so existing accounts need no migration job."""
    if user.media_key_encrypted is None:
        user.media_key_encrypted = wrap_user_media_key(generate_user_media_key())
        user.media_key_version = 1
    return unwrap_user_media_key(user.media_key_encrypted)


async def persist_managed_asset(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    staged_path: Path,
    checksum: str,
    file_size: int,
    mime_type: str,
    filename: str,
    file_created_at: datetime | None = None,
    file_modified_at: datetime | None = None,
) -> tuple[Asset, bool]:
    existing = await session.scalar(select(Asset).where(Asset.user_id == user_id, Asset.checksum == checksum))
    if existing is not None:
        staged_path.unlink(missing_ok=True)
        return existing, True

    user = await session.get(User, user_id)
    if user is None:
        staged_path.unlink(missing_ok=True)
        raise ValueError("Cannot store media for a missing account")
    encryption_version = 1 if settings.media_encryption_enabled else 0
    final_path = original_path_for(checksum, filename, user_id if encryption_version else None)
    if encryption_version:
        commit_encrypted_original(staged_path, final_path, user_media_key(user))
    else:
        commit_original(staged_path, final_path)
    asset = Asset(
        user_id=user_id,
        original_path=str(final_path),
        checksum=checksum,
        storage_checksum=sha256_file(final_path),
        encryption_version=encryption_version,
        file_size=file_size,
        mime_type=mime_type,
        processing_status="pending",
        storage_source="managed",
        original_filename=filename,
        file_created_at=file_created_at,
        file_modified_at=file_modified_at,
    )
    session.add(asset)
    try:
        await session.commit()
        await session.refresh(asset)
        return asset, False
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(Asset).where(Asset.user_id == user_id, Asset.checksum == checksum))
        if existing is None:
            raise
        return existing, True
