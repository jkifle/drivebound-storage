import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user_or_device
from app.db.session import get_db
from app.models.asset import Asset
from app.models.album import Album, AlbumAsset, AlbumMember
from app.models.replica import AssetReplica
from app.models.user import User
from app.schemas.asset import AssetResponse, TimelineAssetResponse, TimelineResponse, UploadResponse
from app.services.ingestion import persist_managed_asset
from app.services.media_delivery import media_response
from app.services.storage import stage_upload, validated_external_path, validated_storage_path
from app.services.timeline import TimelineCursor, decode_cursor, encode_cursor
from app.worker.tasks import process_asset_task, protect_asset_task, restore_asset_task

router = APIRouter(prefix="/assets", tags=["assets"])


async def asset_by_checksum(session: AsyncSession, user_id: uuid.UUID, checksum: str) -> Asset | None:
    return await session.scalar(select(Asset).where(Asset.user_id == user_id, Asset.checksum == checksum))


async def readable_asset(session: AsyncSession, user_id: uuid.UUID, asset_id: uuid.UUID) -> Asset | None:
    """Allow an owner or a member of an album containing the asset to read it."""
    return await session.scalar(
        select(Asset)
        .outerjoin(AlbumAsset, AlbumAsset.asset_id == Asset.id)
        .outerjoin(Album, Album.id == AlbumAsset.album_id)
        .outerjoin(AlbumMember, AlbumMember.album_id == Album.id, full=False)
        .where(
            Asset.id == asset_id,
            or_(Asset.user_id == user_id, Album.user_id == user_id, AlbumMember.user_id == user_id),
        )
        .distinct()
    )


def timeline_statement(user_id: uuid.UUID, limit: int, cursor: TimelineCursor | None = None):
    timeline_at = func.coalesce(Asset.taken_at, Asset.created_at)
    statement = select(Asset).where(Asset.user_id == user_id)
    if cursor is not None:
        statement = statement.where(
            or_(
                timeline_at < cursor.timeline_at,
                and_(timeline_at == cursor.timeline_at, Asset.id < cursor.asset_id),
            )
        )
    return statement.order_by(timeline_at.desc(), Asset.id.desc()).limit(limit + 1)


def timeline_item(asset: Asset) -> TimelineAssetResponse:
    timeline_at = asset.taken_at or asset.created_at
    if timeline_at.tzinfo is None:
        timeline_at = timeline_at.replace(tzinfo=timezone.utc)
    else:
        timeline_at = timeline_at.astimezone(timezone.utc)
    return TimelineAssetResponse(
        id=asset.id,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        duration_seconds=asset.duration_seconds,
        taken_at=asset.taken_at,
        timeline_at=timeline_at,
        day=timeline_at.date(),
        processing_status=asset.processing_status,
        original_url=f"/api/v1/assets/{asset.id}/original",
        thumbnail_url=f"/api/v1/assets/{asset.id}/thumbnail" if asset.thumbnail_path else None,
        original_filename=asset.original_filename,
        protection_status=asset.protection_status,
        restore_status=asset.restore_status,
    )


@router.get("", response_model=TimelineResponse)
async def list_assets(
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = None,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> TimelineResponse:
    decoded_cursor = decode_cursor(cursor) if cursor is not None else None
    assets = list((await session.scalars(timeline_statement(user.id, limit, decoded_cursor))).all())
    has_more = len(assets) > limit
    page = assets[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(TimelineCursor(last.taken_at or last.created_at, last.id))
    return TimelineResponse(
        items=[timeline_item(asset) for asset in page],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    file: UploadFile = File(...),
    file_created_at: datetime | None = Form(default=None),
    file_modified_at: datetime | None = Form(default=None),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> UploadResponse:
    staged_path, checksum, file_size = await stage_upload(file)
    asset, duplicate = await persist_managed_asset(
        session,
        user_id=user.id,
        staged_path=staged_path,
        checksum=checksum,
        file_size=file_size,
        mime_type=file.content_type or "application/octet-stream",
        filename=file.filename or "upload",
        file_created_at=file_created_at,
        file_modified_at=file_modified_at,
    )
    if duplicate:
        return UploadResponse(asset=AssetResponse.model_validate(asset), duplicate=True)

    try:
        process_asset_task.delay(str(asset.id))
    except Exception as exc:
        # The original is already durable and immediately retrievable. Preserve
        # that successful ingestion even if Redis is temporarily unavailable.
        asset.processing_error = f"Could not enqueue processing: {exc}"[:2000]
        await session.commit()

    return UploadResponse(asset=AssetResponse.model_validate(asset), duplicate=False)


@router.get("/protection/status")
async def protection_status(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Asset.protection_status, func.count(Asset.id))
            .where(Asset.user_id == user.id)
            .group_by(Asset.protection_status)
        )
    ).all()
    counts = {state: count for state, count in rows}
    return {
        "total": sum(counts.values()),
        "protected": counts.get("protected", 0),
        "queued": counts.get("queued", 0) + counts.get("protecting", 0),
        "unprotected": counts.get("unprotected", 0),
        "failed": counts.get("failed", 0),
    }


@router.post("/metadata/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_asset_metadata(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> dict[str, int]:
    assets = list((await session.scalars(select(Asset).where(Asset.user_id == user.id))).all())
    queued = 0
    for asset in assets:
        try:
            asset.processing_status = "pending"
            process_asset_task.delay(str(asset.id))
            queued += 1
        except Exception as exc:
            asset.processing_error = f"Could not enqueue metadata re-index: {exc}"[:2000]
    await session.commit()
    return {"queued": queued}


@router.post("/protection/protect-all", status_code=status.HTTP_202_ACCEPTED)
async def protect_all_assets(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> dict[str, int]:
    assets = list((await session.scalars(
        select(Asset)
        .outerjoin(AssetReplica, AssetReplica.asset_id == Asset.id)
        .where(
            Asset.user_id == user.id,
            or_(Asset.protection_status.in_(["unprotected", "failed"]), AssetReplica.metadata_path.is_(None)),
        )
    )).unique().all())
    for asset in assets:
        asset.protection_status = "queued"
    await session.commit()
    queued = 0
    for asset in assets:
        try:
            protect_asset_task.delay(str(asset.id))
            queued += 1
        except Exception:
            asset.protection_status = "failed"
    await session.commit()
    return {"queued": queued}


@router.get("/{asset_id}", response_model=AssetResponse)
async def get_asset(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> Asset:
    asset = await readable_asset(session, user.id, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.post("/{asset_id}/restore", response_model=AssetResponse, status_code=status.HTTP_202_ACCEPTED)
async def restore_asset_copy(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> Asset:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.protection_status != "protected":
        raise HTTPException(status_code=409, detail="A verified protection copy is required before restore")
    asset.restore_status = "queued"
    await session.commit()
    try:
        restore_asset_task.delay(str(asset.id))
    except Exception:
        asset.restore_status = "failed"
        await session.commit()
    return asset


@router.post("/{asset_id}/protect", response_model=AssetResponse, status_code=status.HTTP_202_ACCEPTED)
async def protect_asset_copy(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> Asset:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    asset.protection_status = "queued"
    await session.commit()
    try:
        protect_asset_task.delay(str(asset.id))
    except Exception:
        asset.protection_status = "failed"
        await session.commit()
    return asset


@router.get("/{asset_id}/original")
async def get_original(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> Response:
    asset = await readable_asset(session, user.id, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = (
        validated_external_path(asset.original_path)
        if asset.storage_source == "external"
        else validated_storage_path(asset.original_path, settings.originals_path)
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Original file is unavailable")
    owner = user if asset.user_id == user.id else await session.get(User, asset.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Original file is unavailable")
    return media_response(asset, owner, path, media_type=asset.mime_type, filename=asset.original_filename, content_disposition_type="inline")


@router.get("/{asset_id}/thumbnail")
async def get_thumbnail(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> Response:
    asset = await readable_asset(session, user.id, asset_id)
    if asset is None or asset.thumbnail_path is None:
        raise HTTPException(status_code=404, detail="Thumbnail is not available")
    path = validated_storage_path(asset.thumbnail_path, settings.derivatives_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Thumbnail is not available")
    owner = user if asset.user_id == user.id else await session.get(User, asset.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Thumbnail is not available")
    return media_response(asset, owner, path, media_type="image/webp", content_disposition_type="inline")
