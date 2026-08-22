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
from app.models.upload_session import UploadSession
from app.models.storage_policy import StoragePolicy
from app.models.user import User
from app.services.storage import (
    canonical_storage_path,
    lock_storage_path,
    validated_replica_drive_path,
    validated_storage_path,
)


async def physical_path_has_other_reference(
    session: AsyncSession,
    path: str,
    *,
    excluding_asset_id: uuid.UUID | None = None,
    excluding_replica_id: uuid.UUID | None = None,
    excluding_upload_id: uuid.UUID | None = None,
) -> bool:
    """Check every catalog path kind before unlinking one physical object.

    Roots are administrator-configurable and may overlap.  A replica path can
    therefore also be another account's external original; checking only the
    source column is unsafe.  External originals are always protected, even
    when their own catalog row is the asset currently being purged.
    """
    external_original = await session.scalar(
        select(Asset.id).where(Asset.storage_source == "external", Asset.original_path == path).limit(1)
    )
    if external_original is not None:
        return True
    asset_reference = select(Asset.id).where(
        or_(Asset.original_path == path, Asset.thumbnail_path == path, Asset.preview_path == path)
    )
    if excluding_asset_id is not None:
        asset_reference = asset_reference.where(Asset.id != excluding_asset_id)
    if await session.scalar(asset_reference.limit(1)) is not None:
        return True
    replica_reference = select(AssetReplica.id).where(
        or_(AssetReplica.path == path, AssetReplica.metadata_path == path)
    )
    if excluding_replica_id is not None:
        replica_reference = replica_reference.where(AssetReplica.id != excluding_replica_id)
    if await session.scalar(replica_reference.limit(1)) is not None:
        return True
    upload_reference = select(UploadSession.id).where(UploadSession.staging_path == path)
    if excluding_upload_id is not None:
        upload_reference = upload_reference.where(UploadSession.id != excluding_upload_id)
    if await session.scalar(upload_reference.limit(1)) is not None:
        return True

    # Legacy rows may predate the canonical-path invariant. Exact SQL equality
    # above is the fast path; this bounded deletion-time fallback compares the
    # canonical physical identity across every possible path column so a
    # canonical deleting row cannot unlink another account's ``..``/symlink/
    # Windows-case alias. New publishers share the advisory lock above.
    target_identity = canonical_storage_path(path)

    async def aliases(column, *, excluded_id=None, id_column=None) -> bool:
        statement = select(column).where(column.is_not(None))
        if excluded_id is not None and id_column is not None:
            statement = statement.where(id_column != excluded_id)
        values = (await session.scalars(statement)).all()
        return any(
            not str(value).startswith("duplicate:") and canonical_storage_path(value) == target_identity
            for value in values
        )

    # External originals remain protected even when the derivative being
    # removed belongs to the same Asset row.
    external_aliases = (await session.scalars(
        select(Asset.original_path).where(Asset.storage_source == "external")
    )).all()
    if any(canonical_storage_path(value) == target_identity for value in external_aliases):
        return True
    for column in (Asset.original_path, Asset.thumbnail_path, Asset.preview_path):
        if await aliases(column, excluded_id=excluding_asset_id, id_column=Asset.id):
            return True
    for column in (AssetReplica.path, AssetReplica.metadata_path):
        if await aliases(column, excluded_id=excluding_replica_id, id_column=AssetReplica.id):
            return True
    return await aliases(
        UploadSession.staging_path,
        excluded_id=excluding_upload_id,
        id_column=UploadSession.id,
    )


async def safely_unlink_catalog_path(
    session: AsyncSession,
    path: str,
    *,
    root: Path | None = None,
    replica_root: bool = False,
    excluding_asset_id: uuid.UUID | None = None,
    excluding_replica_id: uuid.UUID | None = None,
    excluding_upload_id: uuid.UUID | None = None,
) -> bool:
    """Reference-aware, path-validated unlink under the shared path lock."""
    resolved = (
        validated_replica_drive_path(path)
        if replica_root
        else validated_storage_path(path, root if root is not None else settings.staging_path)
    )
    # Exact canonical storage is an invariant for new writers.  Never auto-
    # unlink a legacy alias: the exact-string catalog query below could miss a
    # second row naming the same inode through a symlink, ``..``, or case alias.
    if canonical_storage_path(resolved) != path:
        raise ValueError("noncanonical_catalog_path")
    await lock_storage_path(session, resolved)
    if await physical_path_has_other_reference(
        session,
        path,
        excluding_asset_id=excluding_asset_id,
        excluding_replica_id=excluding_replica_id,
        excluding_upload_id=excluding_upload_id,
    ):
        return False
    resolved.unlink(missing_ok=True)
    return True


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
    active_user = await session.scalar(
        select(User)
        .where(User.id == asset.user_id, User.disabled_at.is_(None))
        .with_for_update(read=True)
    )
    if active_user is None:
        raise ValueError("Account is unavailable")
    revisions = list((await session.scalars(
        select(Asset)
        .where(Asset.user_id == asset.user_id, Asset.logical_id == asset.logical_id)
        .order_by(Asset.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    asset = next((revision for revision in revisions if revision.id == asset.id), None)
    if asset is None:
        raise ValueError("This asset no longer exists")
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
    active_user = await session.scalar(
        select(User)
        .where(User.id == target.user_id, User.disabled_at.is_(None))
        .with_for_update(read=True)
    )
    if active_user is None:
        raise ValueError("Account is unavailable")
    revisions = list((await session.scalars(
        select(Asset)
        .where(Asset.user_id == target.user_id, Asset.logical_id == target.logical_id)
        .order_by(Asset.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    current = next((revision for revision in revisions if revision.id == current.id), None)
    target = next((revision for revision in revisions if revision.id == target.id), None)
    if current is None or target is None or current.user_id != target.user_id or current.logical_id != target.logical_id:
        raise ValueError("That revision is not part of this file history")
    if current.lifecycle_state != "active":
        raise ValueError("The current revision cannot be changed while permanent deletion is in progress")
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


async def safely_purge_asset(
    session: AsyncSession,
    asset: Asset,
    *,
    retained_noncanonical: list[str] | None = None,
) -> bool:
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
        try:
            await safely_unlink_catalog_path(
                session, replica.path, replica_root=True, excluding_replica_id=replica.id
            )
        except Exception as exc:
            if retained_noncanonical is not None and str(exc) == "noncanonical_catalog_path":
                retained_noncanonical.append("replica")
                audit(session, asset.user_id, "asset.path_retained", {
                    "asset_id": str(asset.id), "path_kind": "replica", "reason": "noncanonical_legacy_alias",
                })
                return False
            else:
                # Keep the database reference so a later purge pass can retry.
                audit(session, asset.user_id, "asset.purge_deferred", {
                    "asset_id": str(asset.id), "reason": type(exc).__name__, "path_kind": "replica",
                })
                return False
        if replica.metadata_path:
            try:
                await safely_unlink_catalog_path(
                    session, replica.metadata_path, replica_root=True, excluding_replica_id=replica.id
                )
            except Exception as exc:
                if retained_noncanonical is not None and str(exc) == "noncanonical_catalog_path":
                    retained_noncanonical.append("replica_metadata")
                    audit(session, asset.user_id, "asset.path_retained", {
                        "asset_id": str(asset.id), "path_kind": "replica_metadata",
                        "reason": "noncanonical_legacy_alias",
                    })
                    return False
                else:
                    audit(session, asset.user_id, "asset.purge_deferred", {
                        "asset_id": str(asset.id), "reason": type(exc).__name__, "path_kind": "replica_metadata",
                    })
                    return False
        await session.delete(replica)

    if asset.storage_source == "managed":
        try:
            await safely_unlink_catalog_path(
                session, asset.original_path, root=settings.originals_path, excluding_asset_id=asset.id
            )
        except Exception as exc:
            if retained_noncanonical is not None and str(exc) == "noncanonical_catalog_path":
                retained_noncanonical.append("managed_original")
                audit(session, asset.user_id, "asset.path_retained", {
                    "asset_id": str(asset.id), "path_kind": "managed_original",
                    "reason": "noncanonical_legacy_alias",
                })
                return False
            else:
                audit(session, asset.user_id, "asset.purge_deferred", {
                    "asset_id": str(asset.id), "reason": type(exc).__name__, "path_kind": "managed_original",
                })
                return False
    # Thumbnails and previews are always Drivebound-owned, including those
    # generated for read-only external libraries. Do not leak them when the
    # catalog entry is permanently deleted.
    for derivative in {asset.thumbnail_path, asset.preview_path} - {None}:
        try:
            await safely_unlink_catalog_path(
                session, derivative, root=settings.derivatives_path, excluding_asset_id=asset.id
            )
        except Exception as exc:
            if retained_noncanonical is not None and str(exc) == "noncanonical_catalog_path":
                retained_noncanonical.append("derivative")
                audit(session, asset.user_id, "asset.path_retained", {
                    "asset_id": str(asset.id), "path_kind": "derivative", "reason": "noncanonical_legacy_alias",
                })
                return False
            else:
                # Keep the tombstone and retry. Losing this database path would
                # otherwise turn the derivative into an uncollectable orphan.
                audit(session, asset.user_id, "asset.purge_deferred", {
                    "asset_id": str(asset.id), "reason": type(exc).__name__, "path_kind": "derivative",
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
