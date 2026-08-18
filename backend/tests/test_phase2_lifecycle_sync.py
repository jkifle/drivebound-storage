import uuid
import json
import hashlib
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.api.v1.storage_status import archive_response
from app.api.v1.monitoring import queue_account_verification, verification_lock_active
from app.models.asset import Asset
from app.models.replica import AssetReplica
from app.models.monitoring import MonitoringEvent
from app.models.storage_policy import BackupArchive
from app.schemas.sync import SyncOperationInput
from app.services.media_groups import hamming_distance, normalized_stem
from app.services.ingestion import ingestion_lock_key
from app.services.operational_backups import _postgres_connection, _safe_configuration_document, _same_configuration_snapshot
from app.services.replication import _write_replica_metadata
from app.services.storage import validated_replica_drive_path
from app.services.sync import clone_asset_revision
from app.worker.tasks import (
    NoUsableReplicaError,
    ReplicaCandidate,
    _filesystem_timestamp,
    restore_from_candidates,
)


def test_phase2_routes_are_exposed():
    paths = app.openapi()["paths"]
    assert "/api/v1/assets/{asset_id}/trash" in paths
    assert "delete" in paths["/api/v1/assets/{asset_id}"]
    assert "/api/v1/assets/{asset_id}/versions" in paths
    assert "/api/v1/storage/policy" in paths
    assert "/api/v1/storage/drives" in paths
    assert "/api/v1/sync/roots/{root_id}/operations" in paths
    assert "/api/v1/media-groups" in paths
    assert "/api/v1/storage/recovery" in paths
    assert "/api/v1/monitoring/verify" in paths


def test_sync_operation_rejects_path_escape():
    with pytest.raises(ValueError):
        SyncOperationInput(
            operation_id=uuid.uuid4(), client_sequence=1, kind="upsert", logical_id=uuid.uuid4(),
            relative_path="../outside.jpg", asset_id=uuid.uuid4(),
        )


def test_sync_operation_accepts_a_safe_relative_move():
    operation = SyncOperationInput(
        operation_id=uuid.uuid4(), client_sequence=1, kind="move", logical_id=uuid.uuid4(),
        base_revision="r2", relative_path="Pictures/2026/Photo.jpg",
    )
    assert operation.relative_path == "Pictures/2026/Photo.jpg"


def test_private_near_duplicate_helpers_are_deterministic():
    assert hamming_distance("0000000000000000", "000000000000000f") == 4
    assert normalized_stem("IMG_1234 (2).JPG") == "img1234"


def test_replica_paths_must_stay_in_administrator_roots(monkeypatch, tmp_path):
    from app.services import storage

    allowed = tmp_path / "replicas"
    allowed.mkdir()
    monkeypatch.setattr(storage.settings, "replica_roots", str(allowed))
    assert validated_replica_drive_path(str(allowed)) == allowed.resolve()
    with pytest.raises(Exception):
        validated_replica_drive_path(str(tmp_path / "outside"))


def test_asset_history_has_database_invariants():
    constraint_names = {constraint.name for constraint in Asset.__table__.constraints}
    index_names = {index.name for index in Asset.__table__.indexes}

    assert "uq_assets_logical_version" in constraint_names
    assert "ck_assets_lifecycle_state" in constraint_names
    assert "ck_assets_retention_state" in constraint_names
    assert "uq_assets_active_logical" in index_names

    replica_constraint_names = {constraint.name for constraint in AssetReplica.__table__.constraints}
    replica_index_names = {index.name for index in AssetReplica.__table__.indexes}
    monitoring_index_names = {index.name for index in MonitoringEvent.__table__.indexes}
    path_column = AssetReplica.__table__.columns["path"]
    assert not path_column.unique
    assert "uq_asset_replicas_asset_drive" in replica_constraint_names
    assert "uq_asset_replicas_asset_default_drive" in replica_index_names
    assert "uq_monitoring_account_verification_open" in monitoring_index_names


def test_reusing_content_creates_a_distinct_immutable_revision():
    owner_id = uuid.uuid4()
    source = Asset(
        id=uuid.uuid4(), user_id=owner_id, logical_id=uuid.uuid4(), version=2,
        original_path="/data/originals/object", checksum="a" * 64, storage_checksum="b" * 64,
        file_size=42, mime_type="image/jpeg", lifecycle_state="superseded",
        protection_status="protected", restore_status="restored",
    )
    logical_id = uuid.uuid4()

    revision = clone_asset_revision(source, logical_id, 3)

    assert revision.id != source.id
    assert revision.logical_id == logical_id
    assert revision.version == 3
    assert revision.original_path == source.original_path
    assert revision.checksum == source.checksum
    assert revision.lifecycle_state == "active"
    assert revision.protection_status == "unprotected"
    assert revision.restore_status == "not_requested"


def test_backup_summaries_never_disclose_host_paths_or_checksums():
    archive = BackupArchive(
        id=uuid.uuid4(), kind="database", path="/data/backups/private.dump", checksum="f" * 64,
        size_bytes=100, status="created", verified_at=None, created_at=datetime.now(timezone.utc),
        verification_status="failed",
        verification_detail="pg_restore failed for C:\\private\\database.dump on db.internal",
    )

    payload = archive_response(archive).model_dump(mode="json")

    assert "path" not in payload
    assert "checksum" not in payload
    assert "/data/backups" not in json.dumps(payload)
    assert "private" not in json.dumps(payload)
    assert "db.internal" not in json.dumps(payload)
    public_fields = app.openapi()["components"]["schemas"]["BackupArchiveResponse"]["properties"]
    assert "path" not in public_fields
    assert "checksum" not in public_fields


def test_operational_backup_summaries_require_an_account_session():
    client = TestClient(app)

    assert client.get("/api/v1/storage/backups").status_code == 401
    assert client.get("/api/v1/storage/recovery").status_code == 401


def test_pg_dump_password_is_not_exposed_in_process_arguments(monkeypatch):
    from app.services import operational_backups

    monkeypatch.setattr(
        operational_backups.settings,
        "database_url",
        "postgresql+asyncpg://backup-user:very-secret@db.internal:5433/drivebound?sslmode=require",
    )

    command, environment = _postgres_connection()

    assert all("very-secret" not in argument for argument in command)
    assert environment["PGPASSWORD"] == "very-secret"
    assert environment["PGSSLMODE"] == "require"


def test_configuration_snapshot_dedup_ignores_snapshot_timestamp(tmp_path: Path):
    first = _safe_configuration_document()
    first["created_at"] = "2026-01-01T00:00:00+00:00"
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(first), encoding="utf-8")

    assert _same_configuration_snapshot(path)


def test_same_account_and_checksum_share_an_ingestion_lock_only_with_each_other():
    owner = uuid.uuid4()
    checksum = "a" * 64

    assert ingestion_lock_key(owner, checksum) == ingestion_lock_key(owner, checksum)
    assert ingestion_lock_key(owner, checksum) != ingestion_lock_key(uuid.uuid4(), checksum)
    assert ingestion_lock_key(owner, checksum) != ingestion_lock_key(owner, "b" * 64)


def test_shared_replica_bytes_keep_revision_specific_recovery_sidecars(tmp_path: Path):
    destination = tmp_path / "checksum-object"
    destination.write_bytes(b"shared encrypted bytes")
    common = {
        "user_id": uuid.uuid4(), "original_path": "/data/originals/object", "checksum": "a" * 64,
        "storage_checksum": "b" * 64, "file_size": 22, "mime_type": "image/jpeg",
    }
    first = Asset(id=uuid.uuid4(), logical_id=uuid.uuid4(), version=1, **common)
    second = Asset(id=uuid.uuid4(), logical_id=first.logical_id, version=2, **common)

    first_sidecar = _write_replica_metadata(first, destination)
    second_sidecar = _write_replica_metadata(second, destination)

    assert first_sidecar != second_sidecar
    assert first_sidecar.is_file()
    assert second_sidecar.is_file()
    assert str(first.id) in first_sidecar.name
    assert str(second.id) in second_sidecar.name


def test_restore_tries_replicas_in_order_and_atomically_uses_first_valid_copy(tmp_path: Path):
    corrupt_id, healthy_id = uuid.uuid4(), uuid.uuid4()
    corrupt = tmp_path / "corrupt.replica"
    healthy = tmp_path / "healthy.replica"
    destination = tmp_path / "managed" / "original"
    corrupt.write_bytes(b"corrupt")
    healthy.write_bytes(b"the complete encrypted original")
    destination.parent.mkdir()
    destination.write_bytes(b"old damaged bytes")
    expected = hashlib.sha256(healthy.read_bytes()).hexdigest()
    timestamp = datetime(2024, 6, 1, 12, 30, tzinfo=timezone.utc).timestamp()

    selected, failures = restore_from_candidates(
        [ReplicaCandidate(corrupt_id, corrupt), ReplicaCandidate(healthy_id, healthy)],
        destination,
        expected,
        timestamp,
    )

    assert selected == healthy_id
    assert [(failure.id, failure.reason) for failure in failures] == [(corrupt_id, "checksum_mismatch")]
    assert destination.read_bytes() == healthy.read_bytes()
    assert abs(destination.stat().st_mtime - timestamp) < 1
    assert not list(destination.parent.glob("*.restore"))


def test_restore_reports_failure_only_after_every_candidate_fails(tmp_path: Path):
    missing_id, corrupt_id = uuid.uuid4(), uuid.uuid4()
    missing = tmp_path / "missing.replica"
    corrupt = tmp_path / "corrupt.replica"
    corrupt.write_bytes(b"wrong bytes")
    destination = tmp_path / "original"
    expected = hashlib.sha256(b"expected bytes").hexdigest()

    with pytest.raises(NoUsableReplicaError) as captured:
        restore_from_candidates(
            [ReplicaCandidate(missing_id, missing), ReplicaCandidate(corrupt_id, corrupt)],
            destination,
            expected,
        )

    assert [(failure.id, failure.reason) for failure in captured.value.failures] == [
        (missing_id, "missing"),
        (corrupt_id, "checksum_mismatch"),
    ]
    assert not destination.exists()


def test_recovery_verification_queue_is_account_scoped_and_non_destructive(monkeypatch):
    from app.api.v1 import monitoring

    owner_id = uuid.uuid4()
    recorded: list[tuple[str, bool, str]] = []

    class FakeSession:
        def __init__(self, existing=None):
            self.existing = existing
            self.added = []

        async def scalar(self, _statement):
            return self.existing

        async def flush(self):
            return None

        def add(self, value):
            self.added.append(value)

        async def commit(self):
            return None

        async def rollback(self):
            return None

    def delay(user_id: str, repair: bool, verification_id: str):
        recorded.append((user_id, repair, verification_id))
        return SimpleNamespace(id="verification-job")

    monkeypatch.setattr(monitoring.monitor_storage_task, "delay", delay)

    response = asyncio.run(queue_account_verification(FakeSession(), SimpleNamespace(id=owner_id), repair=False))

    assert recorded == [(str(owner_id), False, str(response.verification_id))]
    assert response.model_dump() == {
        "status": "queued",
        "scope": "account",
        "mode": "verify_only",
        "verification_id": response.verification_id,
        "job_id": "verification-job",
    }


def test_recovery_verification_rejects_a_concurrent_account_scan(monkeypatch):
    from app.api.v1 import monitoring

    owner_id = uuid.uuid4()
    active = MonitoringEvent(
        id=uuid.uuid4(),
        user_id=owner_id,
        kind="account_storage_verification",
        severity="info",
        status="open",
        message="running",
        created_at=datetime.now(timezone.utc),
    )

    class FakeSession:
        async def scalar(self, _statement):
            return active

    monkeypatch.setattr(
        monitoring.monitor_storage_task,
        "delay",
        lambda *_args: pytest.fail("a second full scan must not be queued"),
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(queue_account_verification(FakeSession(), SimpleNamespace(id=owner_id), repair=False))

    assert captured.value.status_code == 409


def test_abandoned_recovery_verification_lock_expires():
    event = MonitoringEvent(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="account_storage_verification",
        severity="info",
        status="open",
        message="running",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert not verification_lock_active(event, datetime(2026, 1, 1, 2, tzinfo=timezone.utc))

    event.detail = {"job_id": "still-owned-by-worker"}
    assert verification_lock_active(event, datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_recovery_verification_endpoint_requires_authentication():
    assert TestClient(app).post("/api/v1/monitoring/verify").status_code == 401


def test_naive_preserved_timestamps_are_interpreted_as_utc():
    naive = datetime(2024, 1, 2, 3, 4, 5)
    aware = naive.replace(tzinfo=timezone.utc)

    assert _filesystem_timestamp(naive) == aware.timestamp()
