"""Disposable byte-level regressions; no database, broker, or real disk required."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.api.v1 import storage_status
from app.core.config import settings
from app.models.asset import Asset
from app.models.monitoring import MonitoringEvent
from app.models.replica import AssetReplica
from app.models.storage_policy import BackupArchive
from app.services import operational_backups as backups
from app.services import storage
from app.services.host_storage import StorageUnavailableError
from app.worker import tasks
from test_pilot_destructive_guards import approved_storage  # noqa: F401


def make_archive(paths, kind="database"):
    path = paths["backups"] / f"{kind}-{uuid.uuid4().hex}.fixture"
    path.write_bytes(b"disposable logical archive fixture")
    now = datetime.now(timezone.utc)
    return BackupArchive(
        id=uuid.uuid4(), kind=kind, path=str(path), checksum=storage.sha256_file(path),
        size_bytes=path.stat().st_size, created_at=now, verified_at=now,
        status="created", verification_status="verified",
    )


@pytest.mark.parametrize("failure", ["missing", "corrupt", "wrong_drive"])
def test_previous_verification_does_not_hide_archive_loss(approved_storage, failure):
    archive = make_archive(approved_storage)
    assert asyncio.run(backups.archive_evidence_current(archive))
    if failure == "missing":
        Path(archive.path).unlink()
    elif failure == "corrupt":
        Path(archive.path).write_bytes(b"corrupted fixture")
    else:
        (approved_storage["backups"] / ".drivebound-volume").write_text("wrong-test-drive")
    assert not asyncio.run(backups.archive_evidence_current(archive))


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_backup_now_replaces_a_lost_recent_archive(approved_storage, monkeypatch, failure):
    monkeypatch.setattr(settings, "database_backup_enabled", True)
    old = make_archive(approved_storage)
    if failure == "missing":
        Path(old.path).unlink()
    else:
        Path(old.path).write_bytes(b"invalid fixture")
    dump = Mock(side_effect=lambda path: path.write_bytes(b"replacement archive fixture"))
    monkeypatch.setattr(backups, "_create_database_dump", dump)
    monkeypatch.setattr(backups, "_verify_database_dump", lambda path: "fixture structure checked")
    session = SimpleNamespace(scalar=AsyncMock(return_value=old), add=Mock(), flush=AsyncMock())
    replacement = asyncio.run(backups.create_database_backup(session))
    assert replacement is not old and old.verification_status == "failed"
    dump.assert_called_once()
    assert asyncio.run(backups.revalidate_archive(replacement))
    assert replacement.verification_status == "verified"


def test_verifier_rechecks_already_verified_archives(approved_storage, monkeypatch):
    archive = make_archive(approved_storage)
    Path(archive.path).write_bytes(b"corrupt archive")
    session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [archive])))
    assert asyncio.run(backups.verify_operational_backups(session)) == 0
    assert archive.verification_status == "failed"
    # The query itself must include previously verified rows, not just pending.
    query = str(session.scalars.call_args.args[0].whereclause)
    assert "verification_status" not in query


def test_recovery_endpoint_checks_current_archive_bytes(approved_storage, monkeypatch):
    monkeypatch.setattr(settings, "database_backup_enabled", True)
    database = make_archive(approved_storage)
    configuration = make_archive(approved_storage, "configuration")
    user = SimpleNamespace(id=uuid.uuid4())

    def response():
        session = SimpleNamespace(scalar=AsyncMock(side_effect=[database, configuration, database, configuration, 0, 0]))
        return asyncio.run(storage_status.recovery_readiness(session, user))

    assert response().recoverable
    Path(database.path).unlink()
    result = response()
    assert not result.recoverable and result.database_backup_status == "failed"


@pytest.mark.parametrize("error", [OSError("disk full"), FileExistsError("existing ciphertext")])
def test_encrypted_publication_keeps_retry_source_on_failure(approved_storage, monkeypatch, error):
    source = approved_storage["staging"] / "upload"
    source.write_bytes(b"complete resumable upload")
    destination = approved_storage["originals"] / "object"
    monkeypatch.setattr(storage, "encrypt_file", Mock(side_effect=error))
    if isinstance(error, FileExistsError):
        assert storage.commit_encrypted_original(source, destination, b"k" * 32) is False
    else:
        with pytest.raises(OSError):
            storage.commit_encrypted_original(source, destination, b"k" * 32)
    assert source.read_bytes() == b"complete resumable upload"


def test_plain_publication_keeps_retry_source_on_destination_failure(approved_storage, monkeypatch):
    source = approved_storage["staging"] / "upload"
    source.write_bytes(b"complete resumable upload")
    destination = approved_storage["originals"] / "object"
    monkeypatch.setattr(storage.shutil, "copyfileobj", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        storage.commit_original(source, destination)
    assert source.read_bytes() == b"complete resumable upload"
    assert not destination.exists()


def test_restore_disconnect_records_failure_then_can_resume(approved_storage, monkeypatch):
    replica_path = approved_storage["replicas"] / "copy"
    replica_path.write_bytes(b"restore this original")
    digest = storage.sha256_file(replica_path)
    asset = Asset(
        id=uuid.uuid4(), user_id=uuid.uuid4(), checksum=digest, storage_checksum=digest,
        original_filename="fixture.jpg", original_path=str(approved_storage["originals"] / "lost"),
        storage_source="managed", encryption_version=0, restore_status="queued",
    )
    replica = AssetReplica(id=uuid.uuid4(), asset_id=asset.id, path=str(replica_path),
                           status="verified", verification_status="verified", failure_count=0)
    event = MonitoringEvent(asset_id=asset.id, kind="automatic_restore", status="open",
                            created_at=datetime.now(timezone.utc), detail={})
    session = MagicMock()
    session.__aenter__.return_value = session
    session.commit = AsyncMock()
    session.scalar = AsyncMock(return_value=event)
    session.scalars = AsyncMock(side_effect=[SimpleNamespace(all=lambda rows=rows: rows)
                                            for rows in ([replica], [replica], [event])])
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(tasks, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(tasks, "async_sessionmaker", lambda *a, **kw: lambda: session)
    monkeypatch.setattr(tasks, "active_asset_for_write", AsyncMock(return_value=(asset, SimpleNamespace(id=asset.user_id))))
    monkeypatch.setattr(tasks, "lock_storage_path", AsyncMock())
    monkeypatch.setattr(tasks, "_open_recovery_event", AsyncMock())
    monkeypatch.setattr(tasks, "notify_user_devices", AsyncMock())
    publisher = Mock()
    monkeypatch.setattr(tasks.restore_asset_task, "delay", publisher)
    marker = approved_storage["originals"] / ".drivebound-volume"
    identity = marker.read_text()
    marker.unlink()
    with pytest.raises(StorageUnavailableError):
        asyncio.run(tasks.restore_asset(asset.id))
    assert asset.restore_status == "failed" and session.commit.await_count == 1
    assert replica.status == "verified"  # An unavailable destination is not a corrupt copy.
    marker.write_text(identity)
    assert asyncio.run(tasks.queue_automatic_restore(session, asset))
    publisher.assert_called_once_with(str(asset.id))
    assert asset.restore_status == "queued"
    assert asyncio.run(tasks.restore_asset(asset.id))
    assert asset.restore_status == "restored" and event.status == "resolved"
    assert storage.sha256_file(Path(asset.original_path)) == digest


def test_automatic_restore_retries_expired_lease_but_not_fresh_queue():
    now = datetime.now(timezone.utc)
    asset = SimpleNamespace(restore_status="queued")
    event = MonitoringEvent(created_at=now, detail={"queued_at": now.isoformat()})
    assert not tasks.automatic_restore_retry_due(asset, event, now)
    assert tasks.automatic_restore_retry_due(asset, event, now + timedelta(minutes=16))
    asset.restore_status = "failed"
    assert tasks.automatic_restore_retry_due(asset, event, now)


def test_broker_failure_leaves_restore_retryable(monkeypatch):
    asset = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), restore_status="failed")
    event = MonitoringEvent(created_at=datetime.now(timezone.utc), detail={})
    session = SimpleNamespace(scalar=AsyncMock(return_value=event), commit=AsyncMock())
    monkeypatch.setattr(tasks.restore_asset_task, "delay", Mock(side_effect=RuntimeError("broker unavailable")))
    assert not asyncio.run(tasks.queue_automatic_restore(session, asset))
    assert asset.restore_status == "failed" and session.commit.await_count == 2
    assert "retry" in event.message.lower()
