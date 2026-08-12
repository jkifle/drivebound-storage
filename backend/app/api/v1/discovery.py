import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.assets import timeline_item
from app.core.security import current_user
from app.db.session import get_db
from app.core.config import settings
from app.core.security import opaque_token, token_digest
from app.models.album import Album, AlbumAsset, AlbumInvite, AlbumMember
from app.models.asset import Asset
from app.models.user import User
from app.schemas.asset import TimelineAssetResponse
from app.schemas.discovery import (AlbumCreate, AlbumInviteCreate, AlbumInviteResponse,
    AlbumMemberResponse, AlbumResponse, AlbumUpdate, FileItem, MapItem)
from app.services.accounts import deliver_account_email

router = APIRouter(tags=["discovery"])


async def album_role(session: AsyncSession, album_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Album, str] | None:
    album = await session.get(Album, album_id)
    if album is None:
        return None
    if album.user_id == user_id:
        return album, "owner"
    member = await session.get(AlbumMember, (album_id, user_id))
    return (album, member.role) if member else None


def require_role(access: tuple[Album, str] | None, minimum: str = "viewer") -> tuple[Album, str]:
    ranks = {"viewer": 0, "editor": 1, "owner": 2}
    if access is None or ranks[access[1]] < ranks[minimum]:
        raise HTTPException(status_code=404 if access is None else 403, detail="Album not found" if access is None else "Album permission denied")
    return access


def file_item(asset: Asset) -> FileItem:
    return FileItem(
        id=asset.id,
        name=asset.original_filename or asset.original_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
        relative_path=asset.relative_path,
        mime_type=asset.mime_type,
        file_size=asset.file_size,
        created_at=asset.created_at,
        taken_at=asset.taken_at,
        file_modified_at=asset.file_modified_at,
        latitude=asset.latitude,
        longitude=asset.longitude,
        protection_status=asset.protection_status,
        original_url=f"/api/v1/assets/{asset.id}/original",
        thumbnail_url=f"/api/v1/assets/{asset.id}/thumbnail" if asset.thumbnail_path else None,
    )


@router.get("/files", response_model=list[FileItem])
async def list_files(
    path: str | None = None,
    query: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[FileItem]:
    statement = select(Asset).where(Asset.user_id == user.id)
    if path:
        statement = statement.where(Asset.relative_path.startswith(path))
    if query:
        statement = statement.where(Asset.original_filename.ilike(f"%{query}%"))
    assets = (await session.scalars(statement.order_by(Asset.original_filename, Asset.created_at.desc()).limit(limit))).all()
    return [file_item(asset) for asset in assets]


@router.get("/search", response_model=list[TimelineAssetResponse])
async def search_assets(
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TimelineAssetResponse]:
    pattern = f"%{query}%"
    assets = (
        await session.scalars(
            select(Asset)
            .where(
                Asset.user_id == user.id,
                or_(
                    Asset.original_filename.ilike(pattern),
                    Asset.relative_path.ilike(pattern),
                    Asset.camera_make.ilike(pattern),
                    Asset.camera_model.ilike(pattern),
                    Asset.mime_type.ilike(pattern),
                    cast(Asset.taken_at, Text).ilike(pattern),
                    cast(Asset.file_modified_at, Text).ilike(pattern),
                    cast(Asset.metadata_json, Text).ilike(pattern),
                ),
            )
            .order_by(func.coalesce(Asset.taken_at, Asset.created_at).desc())
            .limit(limit)
        )
    ).all()
    return [timeline_item(asset) for asset in assets]


@router.get("/map", response_model=list[MapItem])
async def map_assets(
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[MapItem]:
    assets = (
        await session.scalars(
            select(Asset)
            .where(Asset.user_id == user.id, Asset.latitude.is_not(None), Asset.longitude.is_not(None))
            .order_by(func.coalesce(Asset.taken_at, Asset.created_at).desc())
            .limit(limit)
        )
    ).all()
    return [
        MapItem(
            id=asset.id,
            latitude=asset.latitude,
            longitude=asset.longitude,
            taken_at=asset.taken_at,
            thumbnail_url=f"/api/v1/assets/{asset.id}/thumbnail" if asset.thumbnail_path else None,
            name=asset.original_filename or "Untitled media",
        )
        for asset in assets
    ]


@router.get("/memories", response_model=list[TimelineAssetResponse])
async def memories(
    limit: int = Query(default=100, ge=1, le=300),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TimelineAssetResponse]:
    today = datetime.now(timezone.utc)
    assets = (await session.scalars(select(Asset).where(
        Asset.user_id == user.id,
        func.extract("month", func.coalesce(Asset.taken_at, Asset.created_at)) == today.month,
        func.extract("day", func.coalesce(Asset.taken_at, Asset.created_at)) == today.day,
        func.extract("year", func.coalesce(Asset.taken_at, Asset.created_at)) < today.year,
    ).order_by(func.coalesce(Asset.taken_at, Asset.created_at).desc()).limit(limit))).all()
    return [timeline_item(asset) for asset in assets]


@router.post("/albums", response_model=AlbumResponse, status_code=status.HTTP_201_CREATED)
async def create_album(
    payload: AlbumCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> AlbumResponse:
    album = Album(user_id=user.id, name=payload.name, description=payload.description)
    session.add(album)
    await session.commit()
    await session.refresh(album)
    return AlbumResponse.model_validate(album)


@router.get("/albums", response_model=list[AlbumResponse])
async def list_albums(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[AlbumResponse]:
    rows = (
        await session.execute(
            select(Album, func.count(func.distinct(AlbumAsset.asset_id)))
            .outerjoin(AlbumAsset, AlbumAsset.album_id == Album.id)
            .outerjoin(AlbumMember, AlbumMember.album_id == Album.id)
            .where(or_(Album.user_id == user.id, AlbumMember.user_id == user.id))
            .group_by(Album.id)
            .order_by(Album.created_at.desc())
        )
    ).all()
    results = []
    for album, count in rows:
        access = await album_role(session, album.id, user.id)
        cover = await session.scalar(
            select(Asset)
            .join(AlbumAsset, AlbumAsset.asset_id == Asset.id)
            .where(AlbumAsset.album_id == album.id)
            .order_by(func.coalesce(Asset.taken_at, Asset.created_at).desc())
            .limit(1)
        )
        results.append(AlbumResponse(
            **AlbumResponse.model_validate(album).model_dump(exclude={"asset_count", "cover_thumbnail_url", "role"}),
            asset_count=count,
            role=access[1] if access else "viewer",
            cover_thumbnail_url=f"/api/v1/assets/{cover.id}/thumbnail" if cover and cover.thumbnail_path else None,
        ))
    return results


@router.patch("/albums/{album_id}", response_model=AlbumResponse)
async def update_album(
    album_id: uuid.UUID,
    payload: AlbumUpdate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> AlbumResponse:
    album, _ = require_role(await album_role(session, album_id, user.id), "editor")
    if payload.name is not None:
        album.name = payload.name.strip()
    if "description" in payload.model_fields_set:
        album.description = payload.description
    await session.commit()
    await session.refresh(album)
    count = await session.scalar(select(func.count()).select_from(AlbumAsset).where(AlbumAsset.album_id == album.id))
    return AlbumResponse(**AlbumResponse.model_validate(album).model_dump(exclude={"asset_count", "cover_thumbnail_url", "role"}), asset_count=count or 0, role=(await album_role(session, album.id, user.id))[1])


@router.delete("/albums/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_album(
    album_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    album = await session.scalar(select(Album).where(Album.id == album_id, Album.user_id == user.id))
    if album is None:
        raise HTTPException(status_code=404, detail="Album not found")
    await session.delete(album)
    await session.commit()


@router.post("/albums/{album_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def add_album_asset(
    album_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    album, _ = require_role(await album_role(session, album_id, user.id), "editor")
    asset = await session.scalar(select(Asset).where(Asset.id == asset_id, Asset.user_id == user.id))
    if album is None or asset is None:
        raise HTTPException(status_code=404, detail="Album or asset not found")
    if await session.get(AlbumAsset, (album_id, asset_id)) is None:
        session.add(AlbumAsset(album_id=album_id, asset_id=asset_id))
        await session.commit()


@router.get("/albums/{album_id}/assets", response_model=list[TimelineAssetResponse])
async def list_album_assets(
    album_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[TimelineAssetResponse]:
    require_role(await album_role(session, album_id, user.id))
    assets = (
        await session.scalars(
            select(Asset)
            .join(AlbumAsset, AlbumAsset.asset_id == Asset.id)
            .where(AlbumAsset.album_id == album_id)
            .order_by(func.coalesce(Asset.taken_at, Asset.created_at).desc())
        )
    ).all()
    return [timeline_item(asset) for asset in assets]


@router.delete("/albums/{album_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_album_asset(
    album_id: uuid.UUID,
    asset_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    album, _ = require_role(await album_role(session, album_id, user.id), "editor")
    membership = await session.get(AlbumAsset, (album_id, asset_id))
    if membership is not None:
        await session.delete(membership)
        await session.commit()


@router.post("/albums/{album_id}/invites", response_model=AlbumInviteResponse)
async def invite_album_member(
    album_id: uuid.UUID, payload: AlbumInviteCreate,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> AlbumInviteResponse:
    album, _ = require_role(await album_role(session, album_id, user.id), "owner")
    token = opaque_token(40)
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    session.add(AlbumInvite(album_id=album.id, email=payload.email.strip().lower(), role=payload.role,
                            token_hash=token_digest(token), invited_by=user.id, expires_at=expires))
    await session.commit()
    invite_url = f"{settings.app_url.rstrip('/')}/albums/invite/{token}"
    await deliver_account_email(payload.email, f"You're invited to {album.name}", f"Open this link to join the Drivebound album:\n\n{invite_url}")
    return AlbumInviteResponse(message="Invitation sent", development_url=invite_url if settings.email_delivery_mode == "console" else None)


@router.post("/albums/invitations/{token}/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept_album_invite(token: str, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> None:
    now = datetime.now(timezone.utc)
    invite = await session.scalar(select(AlbumInvite).where(AlbumInvite.token_hash == token_digest(token), AlbumInvite.accepted_at.is_(None), AlbumInvite.expires_at > now).with_for_update())
    if invite is None:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
    if invite.email.lower() != user.email.lower():
        raise HTTPException(status_code=403, detail="Sign in with the invited email address")
    if await session.get(AlbumMember, (invite.album_id, user.id)) is None:
        session.add(AlbumMember(album_id=invite.album_id, user_id=user.id, role=invite.role, invited_by=invite.invited_by))
    invite.accepted_at = now
    await session.commit()


@router.get("/albums/{album_id}/members", response_model=list[AlbumMemberResponse])
async def album_members(album_id: uuid.UUID, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> list[AlbumMemberResponse]:
    album, _ = require_role(await album_role(session, album_id, user.id))
    owner = await session.get(User, album.user_id)
    rows = (await session.execute(select(AlbumMember, User).join(User, User.id == AlbumMember.user_id).where(AlbumMember.album_id == album_id))).all()
    results = [AlbumMemberResponse(user_id=album.user_id, email=owner.email, display_name=owner.display_name, role="owner")]
    results.extend(AlbumMemberResponse(user_id=member.user_id, email=member_user.email, display_name=member_user.display_name, role=member.role, joined_at=member.joined_at) for member, member_user in rows)
    return results


@router.delete("/albums/{album_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_album_member(album_id: uuid.UUID, member_id: uuid.UUID, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> None:
    require_role(await album_role(session, album_id, user.id), "owner")
    member = await session.get(AlbumMember, (album_id, member_id))
    if member is not None:
        await session.delete(member)
        await session.commit()
