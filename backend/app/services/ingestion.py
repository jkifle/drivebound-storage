import uuid
import hashlib
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, text
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


def ingestion_lock_key(user_id: uuid.UUID, checksum: str) -> int:
    """Stable signed bigint used to serialize same-account deduplication."""
    digest = hashlib.sha256(user_id.bytes + checksum.encode("ascii")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


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
    logical_id: uuid.UUID | None = None,
    force_new_version: bool = False,
) -> tuple[Asset, bool]:
    if not force_new_version:
        # Migration 0014 permits equal content in different immutable version
        # histories. A transaction-scoped advisory lock preserves ordinary
        # upload deduplication without a global checksum uniqueness constraint.
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ingestion_lock_key(user_id, checksum)})
        existing = await session.scalar(
            select(Asset).where(
                Asset.user_id == user_id,
                Asset.checksum == checksum,
                Asset.lifecycle_state == "active",
            )
        )
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
    item_id = logical_id or uuid.uuid4()
    version = 1
    if logical_id is not None:
        version = int((await session.scalar(
            select(func.coalesce(func.max(Asset.version), 0)).where(Asset.user_id == user_id, Asset.logical_id == logical_id)
        )) or 0) + 1
    asset = Asset(
        user_id=user_id,
        logical_id=item_id,
        version=version,
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
        if force_new_version:
            raise
        existing = await session.scalar(
            select(Asset).where(
                Asset.user_id == user_id,
                Asset.checksum == checksum,
                Asset.lifecycle_state == "active",
            )
        )
        if existing is None:
            raise
        return existing, True
