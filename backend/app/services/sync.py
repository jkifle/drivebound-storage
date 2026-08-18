"""Server-side journal for selective desktop-folder synchronization."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.sync import SyncConflict, SyncItem, SyncOperation, SyncRoot
from app.schemas.sync import SyncOperationInput
from app.services.lifecycle import audit, trash_asset


_REVISION_IDENTITY_COLUMNS = {
    "id", "logical_id", "version", "lifecycle_state", "superseded_at", "trashed_at",
    "purge_after", "deleted_by", "protection_status", "restore_status", "restored_at", "created_at",
}


def clone_asset_revision(source: Asset, logical_id: uuid.UUID, version: int) -> Asset:
    """Create another immutable catalog revision that safely shares content-addressed bytes."""
    values = {
        column.name: getattr(source, column.name)
        for column in Asset.__table__.columns
        if column.name not in _REVISION_IDENTITY_COLUMNS
    }
    return Asset(
        id=uuid.uuid4(),
        **values,
        logical_id=logical_id,
        version=version,
        lifecycle_state="active",
        protection_status="unprotected",
        restore_status="not_requested",
    )


def operation_payload(input: SyncOperationInput, asset: Asset | None) -> dict[str, object]:
    payload: dict[str, object] = {"relative_path": input.relative_path}
    if asset is not None:
        payload.update({
            "asset_id": str(asset.id),
            "original_url": f"/api/v1/assets/{asset.id}/original",
            "filename": asset.original_filename,
            "checksum": asset.checksum,
        })
    return payload


def next_revision(cursor: int, operation_id: uuid.UUID) -> str:
    return f"r{cursor}-{operation_id.hex[:12]}"


async def _apply_item_operation(
    session: AsyncSession,
    root: SyncRoot,
    operation: SyncOperation,
    input: SyncOperationInput,
    item: SyncItem | None,
) -> SyncItem:
    if input.kind == "upsert":
        if input.asset_id is None or input.relative_path is None:
            raise ValueError("An upsert needs an uploaded asset and relative path")
        asset = await session.scalar(
            select(Asset).where(Asset.id == input.asset_id, Asset.user_id == root.user_id).with_for_update()
        )
        if asset is None:
            raise ValueError("The uploaded asset is unavailable")
        highest_version = await session.scalar(
            select(func.coalesce(func.max(Asset.version), 0)).where(Asset.user_id == root.user_id, Asset.logical_id == input.logical_id)
        )
        active_target = await session.scalar(
            select(Asset).where(
                Asset.user_id == root.user_id,
                Asset.logical_id == input.logical_id,
                Asset.lifecycle_state == "active",
            ).with_for_update()
        )
        if active_target is not None and active_target.id != asset.id:
            active_target.lifecycle_state = "superseded"
            active_target.superseded_at = datetime.now(timezone.utc)
            await session.flush()
        # The upload endpoint creates a standalone asset first. Attaching it to
        # a sync logical item promotes it to a new immutable revision.
        if asset.logical_id != input.logical_id:
            referenced_elsewhere = await session.scalar(
                select(SyncItem.id).where(
                    SyncItem.asset_id == asset.id,
                    (SyncItem.root_id != root.id) | (SyncItem.logical_id != input.logical_id),
                ).limit(1)
            )
            has_history = await session.scalar(
                select(Asset.id).where(
                    Asset.user_id == root.user_id,
                    Asset.logical_id == asset.logical_id,
                    Asset.id != asset.id,
                ).limit(1)
            )
            if referenced_elsewhere is not None or has_history is not None:
                asset = clone_asset_revision(asset, input.logical_id, int(highest_version or 0) + 1)
                session.add(asset)
            else:
                asset.logical_id = input.logical_id
                asset.version = int(highest_version or 0) + 1
        elif (
            item is not None
            and item.asset_id != asset.id
            and (active_target is None or active_target.id != asset.id)
        ):
            # Reusing the bytes from an older revision is still a new history
            # event; never reactivate and mutate the immutable old row in place.
            asset = clone_asset_revision(asset, input.logical_id, int(highest_version or 0) + 1)
            session.add(asset)
        asset.lifecycle_state = "active"
        asset.superseded_at = None
        asset.trashed_at = None
        asset.purge_after = None
        await session.flush()
        if item is None:
            item = SyncItem(
                root_id=root.id, logical_id=input.logical_id, asset_id=asset.id,
                relative_path=input.relative_path, revision=operation.revision or "",
            )
            session.add(item)
        else:
            item.asset_id = asset.id
            item.relative_path = input.relative_path
            item.revision = operation.revision or ""
            item.deleted_at = None
        operation.payload = operation_payload(input, asset)
    elif input.kind == "move":
        if item is None or item.deleted_at is not None or input.relative_path is None:
            raise ValueError("A move needs an existing, live sync item and relative path")
        item.relative_path = input.relative_path
        item.revision = operation.revision or ""
        operation.payload = operation_payload(input, await session.get(Asset, item.asset_id) if item.asset_id else None)
    else:  # delete
        if item is None:
            item = SyncItem(root_id=root.id, logical_id=input.logical_id, asset_id=None, relative_path=input.relative_path or "", revision=operation.revision or "")
            session.add(item)
        else:
            if item.asset_id:
                asset = await session.get(Asset, item.asset_id)
                if asset and asset.lifecycle_state == "active":
                    asset = await trash_asset(session, asset, actor=f"sync:{operation.client_id}")
            item.deleted_at = datetime.now(timezone.utc)
            item.revision = operation.revision or ""
        operation.payload = {"relative_path": item.relative_path, "deleted": True}
    return item


async def apply_operations(
    session: AsyncSession,
    root: SyncRoot,
    client_id: str,
    inputs: list[SyncOperationInput],
) -> tuple[list[SyncOperation], list[SyncConflict]]:
    locked_root = await session.scalar(select(SyncRoot).where(SyncRoot.id == root.id).with_for_update())
    if locked_root is None:
        raise ValueError("Sync root is unavailable")
    root = locked_root
    operations: list[SyncOperation] = []
    conflicts: list[SyncConflict] = []
    for input in inputs:
        duplicate = await session.scalar(select(SyncOperation).where(SyncOperation.operation_id == input.operation_id))
        if duplicate is not None:
            operations.append(duplicate)
            continue
        duplicate_sequence = await session.scalar(select(SyncOperation).where(
            SyncOperation.root_id == root.id,
            SyncOperation.client_id == client_id,
            SyncOperation.client_sequence == input.client_sequence,
        ))
        if duplicate_sequence is not None:
            operations.append(duplicate_sequence)
            continue

        root.cursor += 1
        item = await session.scalar(
            select(SyncItem).where(SyncItem.root_id == root.id, SyncItem.logical_id == input.logical_id).with_for_update()
        )
        operation = SyncOperation(
            id=uuid.uuid4(), root_id=root.id, cursor=root.cursor, client_id=client_id, client_sequence=input.client_sequence,
            operation_id=input.operation_id, kind=input.kind, logical_id=input.logical_id,
            base_revision=input.base_revision, revision=next_revision(root.cursor, input.operation_id), payload={}, status="applied",
        )
        session.add(operation)
        conflict_kind: str | None = None
        if item is not None and input.base_revision != item.revision:
            conflict_kind = "delete_vs_change" if input.kind == "delete" or item.deleted_at is not None else "concurrent_change"
        if conflict_kind is None:
            try:
                await _apply_item_operation(session, root, operation, input, item)
                audit(session, root.user_id, "sync.operation_applied", {
                    "root_id": str(root.id), "operation_id": str(operation.operation_id), "kind": input.kind,
                    "logical_id": str(input.logical_id), "cursor": operation.cursor,
                })
            except ValueError as exc:
                conflict_kind = "invalid_transition"
                operation.payload = {"relative_path": input.relative_path, "asset_id": str(input.asset_id) if input.asset_id else None, "reason": str(exc)}
        if conflict_kind is not None:
            operation.status = "conflict"
            operation.payload = {
                "relative_path": input.relative_path, "asset_id": str(input.asset_id) if input.asset_id else None,
                "requested_kind": input.kind,
            }
            previous = await session.scalar(
                select(SyncOperation)
                .where(SyncOperation.root_id == root.id, SyncOperation.logical_id == input.logical_id, SyncOperation.status == "applied")
                .order_by(SyncOperation.cursor.desc()).limit(1)
            )
            conflict = SyncConflict(
                root_id=root.id, logical_id=input.logical_id, local_operation_id=operation.id,
                conflicting_operation_id=previous.id if previous else None, kind=conflict_kind,
                detail={"expected_revision": input.base_revision, "actual_revision": item.revision if item else None},
            )
            session.add(conflict)
            conflicts.append(conflict)
            audit(session, root.user_id, "sync.conflict_detected", {
                "root_id": str(root.id), "operation_id": str(operation.operation_id), "kind": conflict_kind,
                "logical_id": str(input.logical_id),
            })
        operations.append(operation)
    root.last_seen_at = datetime.now(timezone.utc)
    await session.flush()
    return operations, conflicts


async def resolve_conflict(session: AsyncSession, conflict: SyncConflict, choice: str) -> SyncConflict:
    if conflict.status != "open":
        return conflict
    operation = await session.get(SyncOperation, conflict.local_operation_id)
    root = await session.get(SyncRoot, conflict.root_id)
    if operation is None or root is None:
        raise ValueError("Sync conflict is incomplete")
    if choice == "keep_local":
        payload = operation.payload
        input = SyncOperationInput(
            operation_id=operation.operation_id, client_sequence=operation.client_sequence, kind=operation.kind,
            logical_id=operation.logical_id, base_revision=None,
            relative_path=payload.get("relative_path") if isinstance(payload.get("relative_path"), str) else None,
            asset_id=uuid.UUID(str(payload["asset_id"])) if payload.get("asset_id") else None,
        )
        item = await session.scalar(select(SyncItem).where(SyncItem.root_id == root.id, SyncItem.logical_id == operation.logical_id))
        await _apply_item_operation(session, root, operation, input, item)
        # The conflicted operation may already have been pulled by peers. Give
        # its resolved form a new cursor so reconnecting clients receive it.
        root.cursor += 1
        operation.cursor = root.cursor
        operation.revision = next_revision(root.cursor, operation.operation_id)
        item = await session.scalar(select(SyncItem).where(SyncItem.root_id == root.id, SyncItem.logical_id == operation.logical_id))
        if item is not None:
            item.revision = operation.revision
        operation.status = "applied"
    else:
        operation.status = "rejected"
    conflict.status = "resolved"
    conflict.resolution = choice
    conflict.resolved_at = datetime.now(timezone.utc)
    audit(session, root.user_id, "sync.conflict_resolved", {
        "conflict_id": str(conflict.id), "choice": choice, "operation_id": str(operation.operation_id),
    })
    return conflict
