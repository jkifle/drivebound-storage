import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.services.storage import commit_original, original_path_for


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

    final_path = original_path_for(checksum, filename)
    commit_original(staged_path, final_path)
    asset = Asset(
        user_id=user_id,
        original_path=str(final_path),
        checksum=checksum,
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
