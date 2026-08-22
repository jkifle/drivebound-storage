import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.assets import lock_active_asset_writer
from app.models.asset import Asset
from app.models.sync import SyncConflict, SyncRoot
from app.models.user import User
from app.schemas.sync import SyncOperationInput
from app.services.lifecycle import restore_asset_from_trash, rollback_asset
from app.services.sync import _apply_item_operation, apply_operations, clone_asset_revision, resolve_conflict


def selected_entity(statement):
    return statement.column_descriptions[0].get("entity")


class FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class FakeSession:
    def __init__(self, *, scalar_values=(), scalar_rows=()):
        self.scalar_values = list(scalar_values)
        self.scalar_rows = list(scalar_rows)
        self.calls = []
        self.added = []

    async def scalar(self, statement):
        self.calls.append(("scalar", statement))
        if not self.scalar_values:
            raise AssertionError("Unexpected scalar query")
        return self.scalar_values.pop(0)

    async def scalars(self, statement):
        self.calls.append(("scalars", statement))
        if not self.scalar_rows:
            raise AssertionError("Unexpected scalars query")
        return FakeScalarResult(self.scalar_rows.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        return None


def make_asset(*, owner_id, logical_id, state, version=1):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        logical_id=logical_id,
        lifecycle_state=state,
        version=version,
        superseded_at=None,
        trashed_at=None,
        purge_after=None,
        deleted_by=None,
        checksum="a" * 64,
    )


def test_apply_operations_disabled_account_stops_before_root_lock_or_mutation():
    owner_id = uuid.uuid4()
    root = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id, cursor=9, status="active")
    session = FakeSession(scalar_values=[None])

    with pytest.raises(ValueError, match="Account is unavailable"):
        asyncio.run(apply_operations(session, root, "desktop", []))

    assert root.cursor == 9
    assert len(session.calls) == 1
    _, user_statement = session.calls[0]
    assert selected_entity(user_statement) is User
    assert user_statement._for_update_arg.read is True
    assert "users.disabled_at IS NULL" in str(user_statement)


def test_resolve_conflict_disabled_account_stops_before_any_locked_root_or_asset():
    owner_id = uuid.uuid4()
    conflict = SimpleNamespace(
        id=uuid.uuid4(), root_id=uuid.uuid4(), local_operation_id=uuid.uuid4(), status="open"
    )
    session = FakeSession(scalar_values=[owner_id, None])

    with pytest.raises(ValueError, match="Account is unavailable"):
        asyncio.run(resolve_conflict(session, conflict, "keep_local"))

    assert conflict.status == "open"
    assert len(session.calls) == 2
    assert selected_entity(session.calls[0][1]) is SyncRoot
    assert session.calls[0][1]._for_update_arg is None
    assert selected_entity(session.calls[1][1]) is User
    assert session.calls[1][1]._for_update_arg.read is True


def test_sync_upsert_requires_an_active_asset_and_does_not_mutate_the_item():
    owner_id = uuid.uuid4()
    item = SimpleNamespace(
        asset_id=uuid.uuid4(), relative_path="Pictures/old.jpg", revision="r1", deleted_at=None
    )
    operation = SimpleNamespace(client_id="desktop", revision="r2", payload={})
    root = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id)
    request = SyncOperationInput(
        operation_id=uuid.uuid4(),
        client_sequence=2,
        kind="upsert",
        logical_id=uuid.uuid4(),
        base_revision="r1",
        relative_path="Pictures/new.jpg",
        asset_id=uuid.uuid4(),
    )
    session = FakeSession(scalar_values=[None])

    with pytest.raises(ValueError, match="uploaded asset is unavailable"):
        asyncio.run(_apply_item_operation(session, root, operation, request, item))

    assert item.asset_id != request.asset_id
    assert item.relative_path == "Pictures/old.jpg"
    asset_statement = session.calls[0][1]
    assert selected_entity(asset_statement) is Asset
    assert asset_statement._for_update_arg is not None
    assert "assets.lifecycle_state" in str(asset_statement)


def test_purging_asset_cannot_be_cloned_as_an_active_revision():
    source = Asset(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        logical_id=uuid.uuid4(),
        version=1,
        original_path="/managed/object",
        checksum="a" * 64,
        storage_checksum="b" * 64,
        file_size=1,
        mime_type="image/jpeg",
        lifecycle_state="purging",
    )

    with pytest.raises(ValueError, match="cannot be cloned or reactivated"):
        clone_asset_revision(source, uuid.uuid4(), 2)


def test_restore_disabled_account_stops_before_asset_lock_and_state_change():
    owner_id = uuid.uuid4()
    asset = make_asset(owner_id=owner_id, logical_id=uuid.uuid4(), state="trashed")
    session = FakeSession(scalar_values=[None])

    with pytest.raises(ValueError, match="Account is unavailable"):
        asyncio.run(restore_asset_from_trash(session, asset, actor="web"))

    assert asset.lifecycle_state == "trashed"
    assert len(session.calls) == 1
    assert selected_entity(session.calls[0][1]) is User
    assert session.calls[0][1]._for_update_arg.read is True


def test_lifecycle_endpoint_fence_rejects_a_stale_disabled_principal():
    session = FakeSession(scalar_values=[None])

    with pytest.raises(HTTPException) as captured:
        asyncio.run(lock_active_asset_writer(session, uuid.uuid4()))

    assert captured.value.status_code == 409
    assert len(session.calls) == 1
    assert selected_entity(session.calls[0][1]) is User
    assert session.calls[0][1]._for_update_arg.read is True


def test_restore_locks_active_user_before_refreshing_and_locking_asset_history():
    owner_id = uuid.uuid4()
    logical_id = uuid.uuid4()
    asset = make_asset(owner_id=owner_id, logical_id=logical_id, state="trashed")
    session = FakeSession(
        scalar_values=[SimpleNamespace(id=owner_id, disabled_at=None)],
        scalar_rows=[[asset]],
    )

    restored = asyncio.run(restore_asset_from_trash(session, asset, actor="web"))

    assert restored.lifecycle_state == "active"
    assert [kind for kind, _ in session.calls] == ["scalar", "scalars"]
    assert selected_entity(session.calls[0][1]) is User
    asset_statement = session.calls[1][1]
    assert selected_entity(asset_statement) is Asset
    assert asset_statement._for_update_arg is not None
    assert asset_statement.get_execution_options()["populate_existing"] is True


def test_rollback_refuses_a_purging_target_after_user_then_asset_lock():
    owner_id = uuid.uuid4()
    logical_id = uuid.uuid4()
    current = make_asset(owner_id=owner_id, logical_id=logical_id, state="active", version=2)
    target = make_asset(owner_id=owner_id, logical_id=logical_id, state="purging", version=1)
    session = FakeSession(
        scalar_values=[SimpleNamespace(id=owner_id, disabled_at=None)],
        scalar_rows=[[target, current]],
    )

    with pytest.raises(ValueError, match="cannot be activated"):
        asyncio.run(rollback_asset(session, current, target, actor="web"))

    assert current.lifecycle_state == "active"
    assert target.lifecycle_state == "purging"
    assert [kind for kind, _ in session.calls] == ["scalar", "scalars"]
    assert selected_entity(session.calls[0][1]) is User
    assert selected_entity(session.calls[1][1]) is Asset
    assert session.calls[1][1]._for_update_arg is not None
