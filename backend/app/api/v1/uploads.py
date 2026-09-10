import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_auth, oauth2_scheme, reject_suppressed_account, token_digest
from app.db.session import get_db
from app.models.asset import Asset
from app.models.device import Device
from app.models.upload_session import UploadSession
from app.models.user import User
from app.schemas.asset import AssetResponse
from app.schemas.upload import UploadSessionCreate, UploadSessionResponse
from app.services.ingestion import persist_managed_asset
from app.services.host_storage import require_storage_path, require_storage
from app.services.storage import canonical_storage_path, sha256_file
from app.worker.tasks import process_asset_task

router = APIRouter(prefix="/uploads", tags=["uploads"])


@dataclass(frozen=True)
class UploadPrincipal:
    user_id: uuid.UUID
    device_id: uuid.UUID | None = None


async def upload_principal(
    request: Request,
    x_device_token: str | None = Header(default=None),
    bearer_token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_db),
) -> UploadPrincipal:
    """Authenticate web sessions or a revocable mobile-device credential."""
    if x_device_token:
        device = await session.scalar(select(Device).where(Device.token_hash == token_digest(x_device_token)))
        if device is None:
            raise HTTPException(status_code=401, detail="Invalid device token")
        reject_suppressed_account(device.user_id)
        user = await session.scalar(select(User).where(User.id == device.user_id, User.disabled_at.is_(None)))
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid device token")
        device.last_seen_at = datetime.now(timezone.utc)
        await session.flush()
        return UploadPrincipal(device.user_id, device.id)
    user, _ = await current_auth(request, bearer_token, session)
    return UploadPrincipal(user.id)


def session_response(upload: UploadSession, asset: Asset | None = None, duplicate: bool = False) -> UploadSessionResponse:
    return UploadSessionResponse(
        id=upload.id,
        upload_url=f"/api/v1/uploads/{upload.id}",
        offset=upload.offset,
        total_size=upload.total_size,
        chunk_size=settings.resumable_chunk_size,
        status=upload.status,
        asset=AssetResponse.model_validate(asset) if asset else None,
        duplicate=duplicate,
    )


@router.post("", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_upload(
    payload: UploadSessionCreate,
    session: AsyncSession = Depends(get_db),
    principal: UploadPrincipal = Depends(upload_principal),
) -> UploadSessionResponse:
    require_storage("originals", "staging")
    if payload.total_size > settings.max_upload_size:
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
    owner = await session.scalar(
        select(User)
        .where(User.id == principal.user_id, User.disabled_at.is_(None))
        .with_for_update(read=True)
    )
    if owner is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    if payload.checksum:
        existing = await session.scalar(
            select(Asset).where(
                Asset.user_id == principal.user_id,
                Asset.checksum == payload.checksum,
                Asset.lifecycle_state == "active",
            )
        )
        if existing is not None:
            placeholder = UploadSession(
                id=uuid.uuid4(),
                user_id=principal.user_id,
                device_id=principal.device_id,
                filename=payload.filename,
                mime_type=payload.mime_type,
                total_size=payload.total_size,
                offset=payload.total_size,
                expected_checksum=payload.checksum,
                staging_path=f"duplicate:{uuid.uuid4()}",
                status="complete",
                expires_at=datetime.now(timezone.utc),
            )
            return session_response(placeholder, existing, duplicate=True)

    staging_directory = settings.staging_path / str(principal.user_id)
    staging_directory.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4()
    staging_path = (staging_directory / f"{upload_id}.resumable").resolve()
    async with aiofiles.open(staging_path, "xb"):
        pass
    upload = UploadSession(
        id=upload_id,
        user_id=principal.user_id,
        device_id=principal.device_id,
        filename=payload.filename,
        mime_type=payload.mime_type,
        total_size=payload.total_size,
        expected_checksum=payload.checksum,
        file_created_at=payload.file_created_at,
        file_modified_at=payload.file_modified_at,
        staging_path=canonical_storage_path(staging_path),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.upload_session_hours),
    )
    session.add(upload)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        staging_path.unlink(missing_ok=True)
        raise
    await session.refresh(upload)
    return session_response(upload)


@router.head("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_status(
    upload_id: uuid.UUID,
    response: Response,
    session: AsyncSession = Depends(get_db),
    principal: UploadPrincipal = Depends(upload_principal),
) -> None:
    upload = await session.scalar(
        select(UploadSession).where(UploadSession.id == upload_id, UploadSession.user_id == principal.user_id)
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    response.headers["Upload-Offset"] = str(upload.offset)
    response.headers["Upload-Length"] = str(upload.total_size)
    response.headers["Upload-Status"] = upload.status


@router.patch("/{upload_id}", response_model=UploadSessionResponse)
async def append_upload(
    upload_id: uuid.UUID,
    request: Request,
    upload_offset: int = Header(alias="Upload-Offset", ge=0),
    session: AsyncSession = Depends(get_db),
    principal: UploadPrincipal = Depends(upload_principal),
) -> UploadSessionResponse:
    owner = await session.scalar(
        select(User)
        .where(User.id == principal.user_id, User.disabled_at.is_(None))
        .with_for_update(read=True)
    )
    if owner is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    upload = await session.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id, UploadSession.user_id == principal.user_id)
        .with_for_update()
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    if upload.status != "active":
        raise HTTPException(status_code=409, detail=f"Upload session is {upload.status}")
    if upload.expires_at < datetime.now(timezone.utc):
        upload.status = "expired"
        await session.commit()
        raise HTTPException(status_code=410, detail="Upload session expired")
    if upload_offset != upload.offset:
        raise HTTPException(status_code=409, detail="Upload offset does not match", headers={"Upload-Offset": str(upload.offset)})

    staging_path = Path(upload.staging_path)
    require_storage_path(staging_path)
    if not staging_path.is_file():
        raise HTTPException(status_code=410, detail="Staged upload is unavailable")
    written = 0
    if upload.offset < upload.total_size:
        async with aiofiles.open(staging_path, "r+b") as output:
            await output.seek(upload.offset)
            async for chunk in request.stream():
                require_storage_path(staging_path)
                if upload.offset + written + len(chunk) > upload.total_size:
                    raise HTTPException(status_code=413, detail="Chunk exceeds declared upload size")
                await output.write(chunk)
                written += len(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="Empty upload chunk")
        upload.offset += written
        await session.commit()

    if upload.offset < upload.total_size:
        return session_response(upload)

    checksum = await asyncio.to_thread(sha256_file, staging_path)
    if upload.expected_checksum and checksum != upload.expected_checksum:
        upload.status = "failed"
        await session.commit()
        require_storage_path(staging_path)
        staging_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Completed upload checksum does not match")

    asset, duplicate = await persist_managed_asset(
        session,
        user_id=upload.user_id,
        staged_path=staging_path,
        checksum=checksum,
        file_size=upload.total_size,
        mime_type=upload.mime_type,
        filename=upload.filename,
        file_created_at=upload.file_created_at,
        file_modified_at=upload.file_modified_at,
    )
    upload = await session.get(UploadSession, upload_id)
    if upload is not None:
        upload.status = "complete"
        if upload.device_id is not None:
            device = await session.get(Device, upload.device_id)
            if device is not None:
                device.last_backup_at = datetime.now(timezone.utc)
                if not duplicate:
                    device.files_backed_up += 1
                    device.bytes_backed_up += upload.total_size
        await session.commit()
    if not duplicate:
        try:
            process_asset_task.delay(str(asset.id))
        except Exception as exc:
            asset.processing_error = f"Could not enqueue processing: {exc}"[:2000]
            await session.commit()
    return session_response(upload, asset, duplicate)  # type: ignore[arg-type]
