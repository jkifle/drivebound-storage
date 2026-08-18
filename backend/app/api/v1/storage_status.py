import asyncio
import os
import shutil
from pathlib import Path

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user
from app.db.session import get_db
from app.models.asset import Asset
from app.models.storage_policy import BackupArchive, StorageDrive
from app.models.user import User
from app.schemas.lifecycle import (
    BackupArchiveResponse,
    LifecyclePolicyResponse,
    LifecyclePolicyUpdate,
    RecoveryReadinessResponse,
    StorageDriveCreate,
    StorageDriveResponse,
    StorageDriveUpdate,
)
from app.services.lifecycle import storage_policy
from app.services.storage import validated_replica_drive_path
from app.worker.tasks import rebalance_user_storage_task

router = APIRouter(prefix="/storage", tags=["storage"])


def archive_response(archive: BackupArchive) -> BackupArchiveResponse:
    detail = archive.verification_detail
    if archive.status == "failed" or archive.verification_status == "failed":
        # Worker diagnostics can include hostnames, database names, or absolute
        # paths. Those belong in operator logs, not an account-facing API.
        detail = "Backup failed verification; inspect the worker logs as the host operator."
    return BackupArchiveResponse(
        id=archive.id,
        kind=archive.kind,
        size_bytes=archive.size_bytes,
        status=archive.status,
        verification_status=archive.verification_status,
        verified_at=archive.verified_at,
        verification_detail=detail,
        created_at=archive.created_at,
    )


def inspect_root(name: str, path: Path, role: str) -> dict[str, object]:
    resolved = path.resolve()
    try:
        usage = shutil.disk_usage(resolved)
        writable = os.access(resolved, os.W_OK)
        return {
            "name": name,
            "path": str(resolved),
            "role": role,
            "status": "online",
            "smart_status": "unavailable",
            "writable": writable,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }
    except OSError as exc:
        return {
            "name": name,
            "path": str(resolved),
            "role": role,
            "status": "unavailable",
            "smart_status": "unavailable",
            "writable": False,
            "error": str(exc),
        }


@router.get("")
async def storage_status(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    roots = [
        ("Managed originals", settings.originals_path, "managed"),
        ("Protection copies", settings.replica_path, "replica"),
    ]
    roots.extend((f"External library {index + 1}", root, "external") for index, root in enumerate(settings.external_root_list))
    configured = list((await session.scalars(
        select(StorageDrive).where(StorageDrive.user_id == user.id).order_by(StorageDrive.priority.desc(), StorageDrive.name)
    )).all())
    roots.extend((drive.name, Path(drive.path), "replica") for drive in configured)
    drives = await asyncio.gather(*(asyncio.to_thread(inspect_root, name, path, role) for name, path, role in roots))
    online = [drive for drive in drives if drive["status"] == "online"]
    return {
        "status": "ok" if online else "unavailable",
        "drives": drives,
        "smart_available": False,
        "health_note": "Filesystem availability is monitored. SMART requires an optional host-level adapter.",
    }


@router.get("/policy", response_model=LifecyclePolicyResponse)
async def get_policy(
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> LifecyclePolicyResponse:
    return LifecyclePolicyResponse.model_validate(await storage_policy(session, user.id))


@router.put("/policy", response_model=LifecyclePolicyResponse)
async def update_policy(
    payload: LifecyclePolicyUpdate,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> LifecyclePolicyResponse:
    policy = await storage_policy(session, user.id)
    for name, value in payload.model_dump(exclude_none=True).items():
        setattr(policy, name, value)
    await session.commit()
    return LifecyclePolicyResponse.model_validate(policy)


@router.get("/drives", response_model=list[StorageDriveResponse])
async def list_replica_drives(
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> list[StorageDrive]:
    return list((await session.scalars(
        select(StorageDrive).where(StorageDrive.user_id == user.id).order_by(StorageDrive.priority.desc(), StorageDrive.name)
    )).all())


@router.post("/drives", response_model=StorageDriveResponse, status_code=status.HTTP_201_CREATED)
async def add_replica_drive(
    payload: StorageDriveCreate,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> StorageDrive:
    path = validated_replica_drive_path(payload.path)
    # The API container deliberately mounts replica media read-only; the
    # isolated worker is the only process that writes copies. Existence is the
    # safe API-side check, and the worker records actual writability during
    # verification before a drive ever counts toward policy.
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Replica drive path is not an available directory")
    existing = await session.scalar(select(StorageDrive).where(StorageDrive.user_id == user.id, StorageDrive.path == str(path)))
    if existing:
        raise HTTPException(status_code=409, detail="This replica drive is already configured")
    drive = StorageDrive(
        user_id=user.id, name=payload.name.strip(), path=str(path), priority=payload.priority, eligible=payload.eligible,
        verification_status="online", last_verified_at=datetime.now(timezone.utc),
    )
    session.add(drive)
    await session.commit()
    await session.refresh(drive)
    return drive


@router.patch("/drives/{drive_id}", response_model=StorageDriveResponse)
async def update_replica_drive(
    drive_id: uuid.UUID,
    payload: StorageDriveUpdate,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> StorageDrive:
    drive = await session.scalar(select(StorageDrive).where(StorageDrive.id == drive_id, StorageDrive.user_id == user.id))
    if drive is None:
        raise HTTPException(status_code=404, detail="Replica drive not found")
    for name, value in payload.model_dump(exclude_none=True).items():
        setattr(drive, name, value.strip() if name == "name" else value)
    await session.commit()
    return drive


@router.delete("/drives/{drive_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_replica_drive(
    drive_id: uuid.UUID,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> None:
    drive = await session.scalar(select(StorageDrive).where(StorageDrive.id == drive_id, StorageDrive.user_id == user.id))
    if drive is None:
        raise HTTPException(status_code=404, detail="Replica drive not found")
    # Removing a drive from policy never deletes bytes. Balancing/replication
    # will create replacement copies on eligible drives first.
    drive.eligible = False
    await session.commit()


@router.post("/balance", status_code=status.HTTP_202_ACCEPTED)
async def balance_storage(user: User = Depends(current_user)) -> dict[str, str]:
    rebalance_user_storage_task.delay(str(user.id))
    return {"status": "queued"}


@router.get("/backups", response_model=list[BackupArchiveResponse])
async def backup_status(
    session: AsyncSession = Depends(get_db), _user: User = Depends(current_user)
) -> list[BackupArchiveResponse]:
    archives = list((await session.scalars(
        select(BackupArchive).order_by(BackupArchive.created_at.desc()).limit(50)
    )).all())
    return [archive_response(archive) for archive in archives]


@router.get("/recovery", response_model=RecoveryReadinessResponse)
async def recovery_readiness(
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> RecoveryReadinessResponse:
    latest_database = await session.scalar(
        select(BackupArchive).where(BackupArchive.kind == "database").order_by(BackupArchive.created_at.desc()).limit(1)
    )
    latest_configuration = await session.scalar(
        select(BackupArchive).where(BackupArchive.kind == "configuration").order_by(BackupArchive.created_at.desc()).limit(1)
    )
    recoverable_database = await session.scalar(
        select(BackupArchive).where(
            BackupArchive.kind == "database",
            BackupArchive.status == "created",
            BackupArchive.verification_status == "verified",
        ).order_by(BackupArchive.created_at.desc()).limit(1)
    )
    recoverable_configuration = await session.scalar(
        select(BackupArchive).where(
            BackupArchive.kind == "configuration",
            BackupArchive.status == "created",
            BackupArchive.verification_status == "verified",
        ).order_by(BackupArchive.created_at.desc()).limit(1)
    )
    failed = await session.scalar(
        select(func.count()).select_from(BackupArchive).where(
            (BackupArchive.status == "failed") | (BackupArchive.verification_status == "failed")
        )
    )
    purges_in_progress = await session.scalar(
        select(func.count()).select_from(Asset).where(
            Asset.user_id == user.id,
            Asset.lifecycle_state == "purging",
        )
    )
    freshness_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=max(settings.database_backup_interval_hours * 2, 1)
    )
    database_fresh = bool(recoverable_database and recoverable_database.created_at >= freshness_cutoff)
    database_ready = bool(
        recoverable_database
        and database_fresh
    )
    configuration_ready = recoverable_configuration is not None
    return RecoveryReadinessResponse(
        database_backup_enabled=settings.database_backup_enabled,
        recoverable=settings.database_backup_enabled and database_ready and configuration_ready,
        database_backup_fresh=database_fresh,
        database_backup_status=(
            "disabled" if not settings.database_backup_enabled else
            "verified" if database_ready else
            "stale" if recoverable_database and not database_fresh else
            latest_database.verification_status if latest_database else "missing"
        ),
        configuration_backup_status="verified" if configuration_ready else (
            latest_configuration.verification_status if latest_configuration else "missing"
        ),
        latest_database_backup_at=recoverable_database.created_at if recoverable_database else None,
        latest_configuration_backup_at=recoverable_configuration.created_at if recoverable_configuration else None,
        failed_archives=int(failed or 0),
        purges_in_progress=int(purges_in_progress or 0),
        retention_days=settings.database_backup_retention_days,
        note=(
            "Verified archives are recovery inputs. Restoring the live database remains an explicit host-operator action "
            "so an account session cannot overwrite deployment state."
        ),
    )
