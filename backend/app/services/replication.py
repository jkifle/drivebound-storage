"""Policy-driven, checksum-verified replica placement across approved drives."""

import asyncio
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.monitoring import MonitoringEvent
from app.models.replica import AssetReplica
from app.models.storage_policy import StorageDrive
from app.services.lifecycle import storage_policy
from app.services.storage import sha256_file, validated_external_path, validated_replica_drive_path, validated_storage_path


@dataclass(frozen=True)
class ReplicaTarget:
    id: uuid.UUID | None
    name: str
    root: Path
    priority: int


def replica_path_for(target: ReplicaTarget, asset: Asset) -> Path:
    if asset.encryption_version:
        return target.root / str(asset.user_id) / asset.checksum[:2] / asset.checksum
    return target.root / asset.checksum[:2] / asset.checksum


def source_path_for(asset: Asset) -> Path:
    return (
        validated_external_path(asset.original_path)
        if asset.storage_source == "external"
        else validated_storage_path(asset.original_path, settings.originals_path)
    )


def disk_score(target: ReplicaTarget) -> tuple[int, int, str]:
    usage = shutil.disk_usage(target.root)
    used_percent = int((usage.used / usage.total) * 100) if usage.total else 100
    # Priority is explicit; within a priority class leave the fullest drive last.
    return (-target.priority, used_percent, target.name.lower())


async def replica_targets(session: AsyncSession, user_id: uuid.UUID) -> list[ReplicaTarget]:
    drives = list((await session.scalars(
        select(StorageDrive).where(StorageDrive.user_id == user_id, StorageDrive.eligible.is_(True))
    )).all())
    if not drives:
        return [ReplicaTarget(None, "Primary protection drive", settings.replica_path.resolve(), 100)]

    targets: list[ReplicaTarget] = []
    for drive in drives:
        try:
            root = validated_replica_drive_path(drive.path)
            if root.is_dir() and os.access(root, os.W_OK):
                targets.append(ReplicaTarget(drive.id, drive.name, root, drive.priority))
                drive.verification_status = "online"
                drive.last_error = None
                drive.last_verified_at = datetime.now(timezone.utc)
            else:
                drive.verification_status = "unavailable"
                drive.last_error = "Drive is not a writable directory"
                drive.failure_count += 1
        except Exception as exc:
            drive.verification_status = "unavailable"
            drive.last_error = str(exc)[:1000]
            drive.failure_count += 1
    return sorted(targets, key=disk_score)


def _write_replica_metadata(asset: Asset, destination: Path) -> Path:
    # Content bytes are checksum-addressed and can be shared by immutable
    # revisions. Each revision keeps its own recovery sidecar.
    metadata_path = destination.with_name(f"{destination.name}.{asset.id}.metadata.json")
    document = {
        "schema": 2,
        "asset_id": str(asset.id),
        "logical_id": str(asset.logical_id),
        "version": asset.version,
        "checksum": asset.checksum,
        "storage_checksum": asset.storage_checksum,
        "encryption_version": asset.encryption_version,
        "original_filename": asset.original_filename,
        "relative_path": asset.relative_path,
        "mime_type": asset.mime_type,
        "file_size": asset.file_size,
        "taken_at": asset.taken_at.isoformat() if asset.taken_at else None,
        "file_created_at": asset.file_created_at.isoformat() if asset.file_created_at else None,
        "file_modified_at": asset.file_modified_at.isoformat() if asset.file_modified_at else None,
        "embedded_metadata": asset.metadata_json,
    }
    temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, metadata_path)
    return metadata_path


def _copy_verified(source: Path, destination: Path, expected_checksum: str) -> tuple[str, Path]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and sha256_file(destination) != expected_checksum:
        destination.unlink()
    if not destination.exists():
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.replicating")
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=4 * 1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    checksum = sha256_file(destination)
    if checksum != expected_checksum:
        raise ValueError("Replica checksum verification failed")
    return checksum, destination


async def _upsert_replica(session: AsyncSession, asset: Asset, target: ReplicaTarget, source: Path) -> AssetReplica:
    destination = replica_path_for(target, asset)
    checksum, destination = await asyncio.to_thread(_copy_verified, source, destination, asset.storage_checksum)
    metadata_path = await asyncio.to_thread(_write_replica_metadata, asset, destination)
    statement = select(AssetReplica).where(AssetReplica.asset_id == asset.id)
    statement = statement.where(AssetReplica.drive_id.is_(None) if target.id is None else AssetReplica.drive_id == target.id)
    replica = await session.scalar(statement)
    if replica is None:
        replica = AssetReplica(
            asset_id=asset.id,
            drive_id=target.id,
            path=str(destination),
            checksum=checksum,
            metadata_path=str(metadata_path),
            status="verified",
            verification_status="verified",
            verified_at=datetime.now(timezone.utc),
        )
        session.add(replica)
    else:
        replica.path = str(destination)
        replica.checksum = checksum
        replica.metadata_path = str(metadata_path)
        replica.status = "verified"
        replica.verification_status = "verified"
        replica.failure_count = 0
        replica.last_error = None
        replica.verified_at = datetime.now(timezone.utc)
    return replica


async def ensure_asset_replicas(session: AsyncSession, asset: Asset) -> int:
    """Make the configured count of independent, verified replica-drive copies."""
    policy = await storage_policy(session, asset.user_id)
    targets = await replica_targets(session, asset.user_id)
    wanted = min(policy.desired_replica_count, len(targets))
    if wanted < policy.desired_replica_count:
        asset.protection_status = "degraded"
        session.add(MonitoringEvent(
            user_id=asset.user_id,
            asset_id=asset.id,
            kind="replica_capacity_shortfall",
            severity="warning",
            message="Fewer eligible replica drives are online than the selected replica policy requires.",
            detail={"desired": policy.desired_replica_count, "available": len(targets)},
        ))
    source = source_path_for(asset)
    if not source.is_file():
        asset.protection_status = "failed"
        raise FileNotFoundError("Source asset is unavailable for replication")
    verified = 0
    failures: list[str] = []
    for target in targets[:wanted]:
        try:
            await _upsert_replica(session, asset, target, source)
            verified += 1
        except Exception as exc:
            failures.append(f"{target.name}: {exc}")
            if target.id:
                drive = await session.get(StorageDrive, target.id)
                if drive:
                    drive.failure_count += 1
                    drive.verification_status = "failed"
                    drive.last_error = str(exc)[:1000]
            replica = await session.scalar(
                select(AssetReplica).where(
                    AssetReplica.asset_id == asset.id,
                    AssetReplica.drive_id.is_(None) if target.id is None else AssetReplica.drive_id == target.id,
                )
            )
            if replica:
                replica.status = "failed"
                replica.verification_status = "failed"
                replica.failure_count += 1
                replica.last_error = str(exc)[:1000]
    asset.protection_status = "protected" if verified >= policy.desired_replica_count else "degraded"
    if failures:
        session.add(MonitoringEvent(
            user_id=asset.user_id,
            asset_id=asset.id,
            kind="replica_verification_failed",
            severity="warning",
            message="One or more replica copies could not be verified.",
            detail={"failures": failures},
        ))
    return verified


async def rebalance_replicas(session: AsyncSession, user_id: uuid.UUID, limit: int = 25) -> int:
    """Move only verified copies, copying to the destination before deletion."""
    policy = await storage_policy(session, user_id)
    targets = await replica_targets(session, user_id)
    if len(targets) < 2:
        return 0
    occupancy = sorted(((shutil.disk_usage(target.root).used / shutil.disk_usage(target.root).total * 100, target) for target in targets), key=lambda value: value[0])
    low_percent, low = occupancy[0]
    high_percent, high = occupancy[-1]
    if high_percent - low_percent < policy.balance_threshold_percent:
        return 0
    replicas = list((await session.execute(
        select(AssetReplica, Asset)
        .join(Asset, Asset.id == AssetReplica.asset_id)
        .where(Asset.user_id == user_id, AssetReplica.status == "verified", AssetReplica.drive_id == high.id)
        .limit(limit)
    )).all())
    moved = 0
    for replica, asset in replicas:
        exists = await session.scalar(select(AssetReplica.id).where(AssetReplica.asset_id == asset.id, AssetReplica.drive_id == low.id))
        if exists is not None:
            continue
        source = Path(replica.path)
        if not source.is_file():
            continue
        try:
            # Create and verify destination first; this leaves the old replica
            # untouched through every failure path.
            new_replica = await _upsert_replica(session, asset, low, source)
            if new_replica.checksum != asset.storage_checksum:
                continue
            old_path, old_metadata = Path(replica.path), Path(replica.metadata_path) if replica.metadata_path else None
            await session.delete(replica)
            old_path.unlink(missing_ok=True)
            if old_metadata:
                old_metadata.unlink(missing_ok=True)
            moved += 1
        except Exception as exc:
            session.add(MonitoringEvent(
                user_id=user_id, asset_id=asset.id, kind="replica_balance_failed", severity="warning",
                message="Storage balancing kept the existing verified copy after a destination failure.", detail={"reason": str(exc)[:500]},
            ))
    return moved
