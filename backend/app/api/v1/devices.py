import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import opaque_token, token_digest
from app.core.security import current_user
from app.db.session import get_db
from app.models.asset import Asset
from app.models.device import Device
from app.models.user import User
from app.schemas.device import BackupStatus, DeviceCreate, DeviceRegistration, DeviceResponse

router = APIRouter(prefix="/devices", tags=["devices"])


async def authenticated_device(session: AsyncSession, device_token: str | None) -> Device:
    if not device_token:
        raise HTTPException(status_code=401, detail="X-Device-Token is required")
    device = await session.scalar(select(Device).where(Device.token_hash == token_digest(device_token)))
    if device is None:
        raise HTTPException(status_code=401, detail="Invalid device token")
    device.last_seen_at = datetime.now(timezone.utc)
    return device


@router.post("", response_model=DeviceRegistration, status_code=status.HTTP_201_CREATED)
async def register_device(
    payload: DeviceCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> DeviceRegistration:
    token = opaque_token()
    device = Device(
        user_id=user.id,
        name=payload.name,
        platform=payload.platform,
        token_hash=token_digest(token),
    )
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return DeviceRegistration(**DeviceResponse.model_validate(device).model_dump(), token=token)


@router.get("", response_model=list[DeviceResponse])
async def list_devices(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[Device]:
    return list((await session.scalars(select(Device).where(Device.user_id == user.id).order_by(Device.created_at))).all())


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    device = await session.scalar(select(Device).where(Device.id == device_id, Device.user_id == user.id))
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    await session.delete(device)
    await session.commit()


@router.get("/backup-status", response_model=BackupStatus)
async def backup_status(
    checksum: str = Query(min_length=64, max_length=64),
    x_device_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> BackupStatus:
    device = await authenticated_device(session, x_device_token)
    asset = await session.scalar(select(Asset).where(Asset.user_id == device.user_id, Asset.checksum == checksum))
    await session.commit()
    return BackupStatus(exists=asset is not None, asset_id=asset.id if asset else None)
