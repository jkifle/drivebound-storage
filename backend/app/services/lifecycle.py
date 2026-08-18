"""Reversible media lifecycle transitions and auditable permanent deletion."""

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.auth import AuditEvent
from app.models.replica import AssetReplica
from app.models.storage_policy import StoragePolicy
from app.services.storage import validated_replica_drive_path, validated_storage_path


async def storage_policy(session: AsyncSession, user_id: uuid.UUID) -> StoragePolicy:
    policy = await session.scalar(select(StoragePolicy).where(StoragePolicy.user_id == user_id))
    if policy is None:
        policy = StoragePolicy(
            user_id=user_id,
            desired_replica_count=1,
            retention_days=settings.lifecycle_retention_days,
            backup_retention_days=settings.database_backup_retention_days,
        )
        session.add(policy)
        await session.flush()
    return policy


def audit(session: AsyncSession, user_id: uuid.UUID, event_type: str, detail: dict[str, object]) -> None:
    session.add(AuditEvent(user_id=user_id, event_type=event_type, detail=detail))


async def trash_asset(
    session: AsyncSession,
    asset: Asset,
    *,
    actor: str,
    retention_days: int | None = None,
) -> Asset:
    locked = await session.scalar(select(Asset).where(Asset.id == asset.id).with_for_update())
    if locked is None:
        raise ValueError("This asset no longer exists")
    asset = locked
    if asset.lifecycle_state == "trashed":
        return asset
    if asset.lifecycle_state != "active":
        raise ValueError("Only the active revision can be moved to trash")
    policy = await storage_policy(session, asset.user_id)
    days = retention_days if retention_days is not None else policy.retention_days
    if days < 1:
        raise ValueError("Retention must be at least one day")
    now = datetime.now(timezone.utc)
    asset.lifecycle_state = "trashed"
    asset.trashed_at = now
    asset.purge_after = now + timedelta(days=days)
    asset.deleted_by = actor[:128]
    audit(session, asset.user_id, "asset.trashed", {
        "asset_id": str(asset.id), "logical_id": str(asset.logical_id), "purge_after": asset.purge_after.isoformat(), "actor": actor,
    })
    return asset


async def restore_asset_from_trash(session: AsyncSession, asset: Asset, *, actor: str) -> Asset:
    revisions = list((await session.scalars(
        select(Asset)
        .where(Asset.user_id == asset.user_id, Asset.logical_id == asset.logical_id)
        .with_for_update()
    )).all())
    asset = next((revision for revision in revisions if revision.id == asset.id), asset)
    if asset.lifecycle_state != "trashed":
        raise ValueError("This revision is not in trash")
    active = next(
        (revision for revision in revisions if revision.lifecycle_state == "active" and revision.id != asset.id),
        None,
    )
    if active is not None:
        raise ValueError("A newer revision is active; roll back to this revision instead")
    asset.lifecycle_state = "active"
    asset.trashed_at = None
    asset.purge_after = None
    asset.deleted_by = None
    audit(session, asset.user_id, "asset.restored_from_trash", {
        "asset_id": str(asset.id), "logical_id": str(asset.logical_id), "actor": actor,
    })
    return asset


async def rollback_asset(session: AsyncSession, current: Asset, target: Asset, *, actor: str) -> Asset:
    if current.user_id != target.user_id or current.logical_id != target.logical_id:
        raise ValueError("That revision is not part of this file history")
    revisions = list((await session.scalars(
        select(Asset)
        .where(Asset.user_id == target.user_id, Asset.logical_id == target.logical_id)
        .with_for_update()
    )).all())
    target = next((revision for revision in revisions if revision.id == target.id), target)
    if target.lifecycle_state not in {"active", "superseded"}:
        if target.lifecycle_state == "trashed":
            raise ValueError("Restore a trashed revision before rolling back")
        raise ValueError("That revision cannot be activated while permanent deletion is in progress")
    now = datetime.now(timezone.utc)
    previous_ids: list[str] = []
    for revision in revisions:
        if revision.id != target.id and revision.lifecycle_state == "active":
            revision.lifecycle_state = "superseded"
            revision.superseded_at = now
            previous_ids.append(str(revision.id))
    if target.lifecycle_state == "active" and not previous_ids:
        return target
    # The partial unique index allows only one active revision. Publish the
    # deactivation before activating the historical target so statement-level
    # uniqueness checks cannot observe two current rows.
    if previous_ids:
        await session.flush()
    target.lifecycle_state = "active"
    target.superseded_at = None
    target.trashed_at = None
    target.purge_after = None
    target.deleted_by = None
    audit(session, target.user_id, "asset.rolled_back", {
        "from_asset_ids": previous_ids,
        "to_asset_id": str(target.id), "logical_id": str(target.logical_id), "actor": actor,
    })
    return target


async def safely_purge_asset(session: AsyncSession, asset: Asset) -> bool:
    """Delete only Drivebound-owned bytes after the retention window expires.

    Asset revisions can reference the same checksum-addressed file. A byte path
    is removed only once no other Asset/AssetReplica row refers to it.
    """
    now = datetime.now(timezone.utc)
    if asset.lifecycle_state not in {"trashed", "purging"} or asset.purge_after is None or asset.purge_after > now:
        return False
    # ``purging`` is a durable, retryable tombstone. If a worker stops between
    # filesystem cleanup and the database delete, the next scheduled pass can
    # safely finish because every unlink below is idempotent.
    asset.lifecycle_state = "purging"
    await session.flush()

    replicas = list((await session.scalars(select(AssetReplica).where(AssetReplica.asset_id == asset.id))).all())
    for replica in replicas:
        other_reference = await session.scalar(
            select(AssetReplica.id).where(AssetReplica.path == replica.path, AssetReplica.id != replica.id).limit(1)
        )
        if other_reference is None:
            try:
                replica_path = validated_replica_drive_path(replica.path)
                replica_path.unlink(missing_ok=True)
            except Exception as exc:
                # Keep the database reference so a later purge pass can retry.
                audit(session, asset.user_id, "asset.purge_deferred", {"asset_id": str(asset.id), "reason": str(exc)[:500]})
                return False
        if replica.metadata_path:
            other_metadata = await session.scalar(
                select(AssetReplica.id)
                .where(AssetReplica.metadata_path == replica.metadata_path, AssetReplica.id != replica.id)
                .limit(1)
            )
            if other_metadata is None:
                try:
                    validated_replica_drive_path(replica.metadata_path).unlink(missing_ok=True)
                except Exception as exc:
                    audit(session, asset.user_id, "asset.purge_deferred", {
                        "asset_id": str(asset.id), "reason": str(exc)[:500], "path_kind": "replica_metadata",
                    })
                    return False
        await session.delete(replica)

    if asset.storage_source == "managed":
        other_asset = await session.scalar(select(Asset.id).where(Asset.original_path == asset.original_path, Asset.id != asset.id).limit(1))
        if other_asset is None:
            try:
                validated_storage_path(asset.original_path, settings.originals_path).unlink(missing_ok=True)
            except Exception as exc:
                audit(session, asset.user_id, "asset.purge_deferred", {"asset_id": str(asset.id), "reason": str(exc)[:500]})
                return False
    # Thumbnails and previews are always Drivebound-owned, including those
    # generated for read-only external libraries. Do not leak them when the
    # catalog entry is permanently deleted.
    for derivative in {asset.thumbnail_path, asset.preview_path} - {None}:
        other_derivative = await session.scalar(
            select(Asset.id).where(
                (Asset.thumbnail_path == derivative) | (Asset.preview_path == derivative), Asset.id != asset.id
            ).limit(1)
        )
        if other_derivative is None:
            try:
                validated_storage_path(derivative, settings.derivatives_path).unlink(missing_ok=True)
            except Exception as exc:
                # Keep the tombstone and retry. Losing this database path would
                # otherwise turn the derivative into an uncollectable orphan.
                audit(session, asset.user_id, "asset.purge_deferred", {
                    "asset_id": str(asset.id), "reason": str(exc)[:500], "path_kind": "derivative",
                })
                return False

    audit(session, asset.user_id, "asset.purged", {
        "asset_id": str(asset.id), "logical_id": str(asset.logical_id), "checksum": asset.checksum,
    })
    await session.delete(asset)
    return True


async def purge_due_assets(
    session: AsyncSession,
    limit: int = 100,
    *,
    asset_id: uuid.UUID | None = None,
) -> int:
    statement = (
        select(Asset.id)
        .where(
            or_(Asset.lifecycle_state == "trashed", Asset.lifecycle_state == "purging"),
            Asset.purge_after.is_not(None),
            Asset.purge_after <= datetime.now(timezone.utc),
        )
        .order_by(Asset.purge_after)
        .limit(limit)
    )
    if asset_id is not None:
        statement = statement.where(Asset.id == asset_id)
    due_ids = list((await session.scalars(statement)).all())
    purged = 0
    for due_id in due_ids:
        asset = await session.scalar(
            select(Asset)
            .where(
                Asset.id == due_id,
                or_(Asset.lifecycle_state == "trashed", Asset.lifecycle_state == "purging"),
                Asset.purge_after <= datetime.now(timezone.utc),
            )
            .with_for_update(skip_locked=True)
        )
        if asset is None:
            continue
        if await safely_purge_asset(session, asset):
            purged += 1
        # Commit each tombstone independently so one unavailable drive cannot
        # roll back completed cleanup for unrelated assets.
        await session.commit()
    return purged
