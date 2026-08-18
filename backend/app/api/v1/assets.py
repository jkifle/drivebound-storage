import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user, current_user_or_device
from app.db.session import get_db
from app.models.asset import Asset
from app.models.album import Album, AlbumAsset, AlbumMember
from app.models.replica import AssetReplica
from app.models.user import User
from app.schemas.asset import AssetResponse, TimelineAssetResponse, TimelineResponse, UploadResponse
from app.services.ingestion import persist_managed_asset
from app.services.media_delivery import media_response
from app.services.storage import stage_upload, validated_external_path, validated_storage_path
from app.services.lifecycle import restore_asset_from_trash, rollback_asset, trash_asset
from app.schemas.lifecycle import AssetRevisionResponse, TrashResponse
from app.worker.tasks import purge_expired_assets_task
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
            Asset.lifecycle_state == "active",
            or_(Asset.user_id == user_id, Album.user_id == user_id, AlbumMember.user_id == user_id),
        )
        .distinct()
    )


def timeline_statement(user_id: uuid.UUID, limit: int, cursor: TimelineCursor | None = None):
    timeline_at = func.coalesce(Asset.taken_at, Asset.created_at)
    statement = select(Asset).where(Asset.user_id == user_id, Asset.lifecycle_state == "active")
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


def revision_item(asset: Asset) -> AssetRevisionResponse:
    return AssetRevisionResponse(
        id=asset.id, logical_id=asset.logical_id, version=asset.version, lifecycle_state=asset.lifecycle_state,
        original_filename=asset.original_filename, checksum=asset.checksum, file_size=asset.file_size,
        created_at=asset.created_at, superseded_at=asset.superseded_at, trashed_at=asset.trashed_at,
        purge_after=asset.purge_after,
    )


@router.get("/trash", response_model=list[TrashResponse])
async def list_trash(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TrashResponse]:
    assets = list((await session.scalars(
        select(Asset).where(Asset.user_id == user.id, Asset.lifecycle_state == "trashed").order_by(Asset.trashed_at.desc())
    )).all())
    return [TrashResponse(**revision_item(asset).model_dump()) for asset in assets]


@router.post("/{asset_id}/trash", response_model=TrashResponse)
async def move_to_trash(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> TrashResponse:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id, Asset.lifecycle_state == "active"))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        asset = await trash_asset(session, asset, actor="web")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return TrashResponse(**revision_item(asset).model_dump())


@router.delete("/{asset_id}", response_model=TrashResponse)
async def delete_asset(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> TrashResponse:
    """Delete is intentionally reversible until the retention deadline."""
    asset = await session.scalar(
        select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id, Asset.lifecycle_state == "active")
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Active asset not found")
    try:
        asset = await trash_asset(session, asset, actor="web")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return TrashResponse(**revision_item(asset).model_dump())


@router.post("/{asset_id}/restore-from-trash", response_model=AssetRevisionResponse)
async def restore_from_trash(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> AssetRevisionResponse:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        asset = await restore_asset_from_trash(session, asset, actor="web")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return revision_item(asset)


@router.get("/{asset_id}/versions", response_model=list[AssetRevisionResponse])
async def asset_versions(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[AssetRevisionResponse]:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    revisions = list((await session.scalars(
        select(Asset)
        .where(Asset.user_id == user.id, Asset.logical_id == asset.logical_id)
        .order_by(Asset.version.desc(), Asset.created_at.desc())
    )).all())
    return [revision_item(revision) for revision in revisions]


@router.post("/{asset_id}/rollback/{version_id}", response_model=AssetRevisionResponse)
async def rollback_to_version(
    asset_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> AssetRevisionResponse:
    current = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    target = await session.scalar(select(Asset).where(Asset.id == version_id, Asset.user_id == user.id))
    if current is None or target is None:
        raise HTTPException(status_code=404, detail="Asset revision not found")
    try:
        restored = await rollback_asset(session, current, target, actor="web")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return revision_item(restored)


@router.post("/{asset_id}/purge", status_code=status.HTTP_202_ACCEPTED)
async def purge_when_retained(
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, str]:
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.lifecycle_state != "trashed":
        raise HTTPException(status_code=409, detail="Move the asset to trash before requesting permanent deletion")
    if asset.purge_after is None:
        raise HTTPException(status_code=409, detail="The asset has no retention deadline")
    if asset.purge_after > datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="The retention period has not elapsed")
    try:
        purge_expired_assets_task.delay(str(asset.id))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Permanent deletion could not be queued") from exc
    return {"status": "queued", "asset_id": str(asset.id)}


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
    asset = await session.scalar(select(Asset).where(
        Asset.id == asset_id,
        Asset.user_id == user.id,
        Asset.lifecycle_state == "active",
    ))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    usable_replica = await session.scalar(
        select(AssetReplica.id).where(
            AssetReplica.asset_id == asset.id,
            AssetReplica.status == "verified",
            AssetReplica.verification_status == "verified",
        ).limit(1)
    )
    if usable_replica is None:
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
