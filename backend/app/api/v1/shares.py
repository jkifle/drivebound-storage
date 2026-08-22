import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    current_user_or_device,
    hash_password_async,
    opaque_token,
    reject_suppressed_account,
    token_digest,
    verify_password_async,
)
from app.db.session import get_db
from app.models.asset import Asset
from app.models.share import ShareLink
from app.models.user import User
from app.schemas.share import PublicShareResponse, ShareCreate, ShareResponse
from app.services.storage import validated_external_path, validated_storage_path
from app.services.media_delivery import media_response

router = APIRouter(prefix="/shares", tags=["sharing"])


async def resolve_share(session: AsyncSession, token: str, password: str | None) -> tuple[ShareLink, Asset]:
    share = await session.scalar(select(ShareLink).where(ShareLink.token_hash == token_digest(token)))
    if share is None:
        raise HTTPException(status_code=404, detail="Share not found")
    reject_suppressed_account(share.user_id)
    now = datetime.now(timezone.utc)
    expires_at = share.expires_at
    if expires_at and (expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)) <= now:
        raise HTTPException(status_code=410, detail="Share has expired")
    if share.password_hash and (not password or not await verify_password_async(password, share.password_hash)):
        raise HTTPException(status_code=401, detail="Share password is required or incorrect")
    asset = await session.get(Asset, share.asset_id)
    if asset is None or asset.lifecycle_state != "active":
        raise HTTPException(status_code=404, detail="Shared asset not found")
    return share, asset


@router.post("", response_model=ShareResponse, status_code=status.HTTP_201_CREATED)
async def create_share(
    payload: ShareCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> ShareResponse:
    asset = await session.scalar(select(Asset).where(Asset.id == payload.asset_id, Asset.user_id == user.id, Asset.lifecycle_state == "active"))
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    token = opaque_token()
    share = ShareLink(
        user_id=user.id,
        asset_id=payload.asset_id,
        token_hash=token_digest(token),
        password_hash=await hash_password_async(payload.password) if payload.password else None,
        allow_download=payload.allow_download,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=payload.expires_in_hours))
        if payload.expires_in_hours
        else None,
    )
    session.add(share)
    await session.commit()
    await session.refresh(share)
    share_data = ShareResponse.model_validate(share).model_dump(exclude={"password_required", "url"})
    return ShareResponse(
        **share_data,
        password_required=share.password_hash is not None,
        url=f"{settings.app_url.rstrip('/')}/share/{token}",
    )


@router.get("/public/{token}", response_model=PublicShareResponse)
async def public_share(
    token: str,
    x_share_password: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> PublicShareResponse:
    share, asset = await resolve_share(session, token, x_share_password)
    return PublicShareResponse(
        asset_id=asset.id,
        filename=asset.original_filename,
        mime_type=asset.mime_type,
        created_at=asset.created_at,
        expires_at=share.expires_at,
        password_required=share.password_hash is not None,
        allow_download=share.allow_download,
        content_url=f"/api/v1/shares/public/{token}/content",
    )


@router.get("/public/{token}/content")
async def public_share_content(
    token: str,
    x_share_password: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> Response:
    share, asset = await resolve_share(session, token, x_share_password)
    path = (
        validated_external_path(asset.original_path)
        if asset.storage_source == "external"
        else validated_storage_path(asset.original_path, settings.originals_path)
    )
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Shared file is unavailable")
    disposition = "attachment" if share.allow_download else "inline"
    owner = await session.get(User, asset.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Shared file is unavailable")
    return media_response(asset, owner, path, media_type=asset.mime_type, filename=asset.original_filename, content_disposition_type=disposition)
