import asyncio
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
from app.services.encryption import (
    generate_user_media_key,
    iter_decrypted_chunks,
    unwrap_user_media_key,
    wrap_user_media_key,
)
from app.services.storage import (
    commit_encrypted_original,
    commit_original,
    canonical_storage_path,
    lock_storage_path,
    original_path_for,
    sha256_file,
)


def initialize_user_media_key(user: User) -> bytes:
    """Initialize a DEK while the caller holds the User row exclusively.

    The caller must commit this wrapped key before publishing ciphertext.  Key
    creation is intentionally separate from ``user_media_key`` so a future
    read/write caller cannot accidentally recreate the old key-loss window.
    """
    if user.disabled_at is not None:
        raise ValueError("Cannot initialize a media key for a disabled account")
    if user.media_key_encrypted is None:
        user.media_key_encrypted = wrap_user_media_key(generate_user_media_key())
        user.media_key_version = 1
    return unwrap_user_media_key(user.media_key_encrypted)


def user_media_key(user: User) -> bytes:
    """Unwrap an already-durable account DEK without mutating the account."""
    if user.disabled_at is not None:
        raise ValueError("Cannot unwrap a media key for a disabled account")
    if user.media_key_encrypted is None:
        raise ValueError("Account media key has not been initialized")
    return unwrap_user_media_key(user.media_key_encrypted)


def ingestion_lock_key(user_id: uuid.UUID, checksum: str) -> int:
    """Stable signed bigint used to serialize same-account deduplication."""
    digest = hashlib.sha256(user_id.bytes + checksum.encode("ascii")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def encrypted_plaintext_checksum(path: Path, key: bytes, expected_size: int | None = None) -> str:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter_decrypted_chunks(path, key):
        digest.update(chunk)
        size += len(chunk)
    if expected_size is not None and size != expected_size:
        raise ValueError("Existing encrypted object has an unexpected plaintext size")
    return digest.hexdigest()


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
    # User -> advisory content/path lock -> Asset is the global ingestion lock
    # order. Account deletion takes User FOR UPDATE, so it waits for any writer
    # that already started and every losing writer observes ``disabled_at``.
    # Exclusive because the first upload may initialize the per-account DEK;
    # two concurrent first uploads must never publish under different keys.
    user = await session.scalar(
        select(User).where(User.id == user_id, User.disabled_at.is_(None)).with_for_update()
    )
    if user is None:
        staged_path.unlink(missing_ok=True)
        raise ValueError("Cannot store media for a missing or disabled account")
    encryption_version = 1 if settings.media_encryption_enabled else 0
    if encryption_version and user.media_key_encrypted is None:
        # The wrapped DEK must be durable before any ciphertext is published.
        # A crash after file publication can then be retried with the same key
        # rather than silently adopting bytes encrypted under a discarded key.
        initialize_user_media_key(user)
        await session.commit()
        user = await session.scalar(
            select(User).where(User.id == user_id, User.disabled_at.is_(None)).with_for_update()
        )
        if user is None:
            staged_path.unlink(missing_ok=True)
            raise ValueError("Cannot store media for a missing or disabled account")
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

    final_path = original_path_for(checksum, filename, user_id if encryption_version else None).resolve()
    await lock_storage_path(session, final_path)
    if encryption_version:
        key = user_media_key(user)
        created = commit_encrypted_original(staged_path, final_path, key)
        if not created and await asyncio.to_thread(
            encrypted_plaintext_checksum, final_path, key, file_size
        ) != checksum:
            raise ValueError("Existing encrypted original does not authenticate as the uploaded content")
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
        original_path=canonical_storage_path(final_path),
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
