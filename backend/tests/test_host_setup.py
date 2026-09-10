import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

from app.api.v1 import health, libraries, storage_status
from app.core.config import Settings, settings
from app.models.external_library import ExternalLibrary
from app.models.user import User
from app.schemas.library import LibraryConnect
from app.services import host_storage, readiness
from test_pilot_destructive_guards import approved_storage  # noqa: F401


def owner(**overrides):
    return User(id=uuid.uuid4(), email="owner@example.test", email_verified_at=datetime.now(timezone.utc), **overrides)


@pytest.mark.parametrize("email,verified,disabled,allowed", [
    ("owner@example.test", True, False, True),
    ("OWNER@example.test", True, False, True),
    ("other@example.test", True, False, False),
    ("owner@example.test", False, False, False),
    ("owner@example.test", True, True, False),
])
def test_import_authority_is_verified_host_approval(approved_storage, email, verified, disabled, allowed):
    now = datetime.now(timezone.utc)
    user = User(id=uuid.uuid4(), email=email, email_verified_at=now if verified else None, disabled_at=now if disabled else None)
    assert host_storage.is_host_owner(user) is allowed


def test_missing_manifest_and_unbound_paths_fail_closed(approved_storage, tmp_path):
    with pytest.raises(host_storage.StorageUnavailableError):
        host_storage.require_storage_path(tmp_path / "another-drive")
    settings.storage_manifest_path.unlink()
    with pytest.raises(host_storage.StorageUnavailableError):
        host_storage.require_storage("originals")


def test_unconfigured_manual_deployment_not_credited_with_verified_disk(monkeypatch):
    monkeypatch.setattr(settings, "storage_manifest_path", None)
    monkeypatch.setattr(settings, "host_owner_email", None)
    assert host_storage.storage_readiness() == {"identity": "not_configured"}
    with pytest.raises(HTTPException) as error:
        host_storage.require_host_owner(owner())
    assert error.value.status_code == 409
    assert Settings(_env_file=None, storage_manifest_path="").storage_manifest_path is None


def test_manifest_with_missing_role_cannot_skip_checks(approved_storage):
    manifest = json.loads(settings.storage_manifest_path.read_text())
    manifest["bindings"].pop()
    settings.storage_manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(host_storage.StorageUnavailableError):
        host_storage.require_storage("originals")


def test_setup_does_not_disclose_host_paths_to_other_account(approved_storage):
    session = SimpleNamespace(scalar=AsyncMock(side_effect=AssertionError("Must not query private host library")))
    user = owner()
    user.email = "other@example.test"
    result = asyncio.run(libraries.library_setup(session, user))
    assert not result.can_connect
    assert result.folder_name is None and result.library is None
    assert str(approved_storage["imports"]) not in result.model_dump_json()


def test_connect_reuses_library_for_retry_instead_of_duplicate(approved_storage):
    user = owner()
    root = approved_storage["imports"].resolve()
    library = ExternalLibrary(id=uuid.uuid4(), user_id=user.id, name="First", path=str(root))
    session = SimpleNamespace(scalar=AsyncMock(return_value=user), scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [library])))
    result = asyncio.run(libraries.connect_library(LibraryConnect(name="Retry"), session, user))
    assert result is library


def test_legacy_other_account_overlap_is_not_reassigned(approved_storage):
    user = owner()
    root = approved_storage["imports"].resolve()
    library = ExternalLibrary(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Other", path=str(root))
    session = SimpleNamespace(scalar=AsyncMock(return_value=user), scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [library])))
    with pytest.raises(HTTPException) as error:
        asyncio.run(libraries.connect_library(LibraryConnect(name="Owner"), session, user))
    assert error.value.status_code == 409


def test_scan_publish_failure_is_visible_redacted_and_retryable(approved_storage, monkeypatch):
    user = owner()
    library = ExternalLibrary(id=uuid.uuid4(), user_id=user.id, path=str(approved_storage["imports"]), name="Photos")
    session = SimpleNamespace(scalar=AsyncMock(return_value=library), commit=AsyncMock())
    def fail(*args):
        raise RuntimeError("redis://secret-password@private-host")
    monkeypatch.setattr(libraries.scan_library_task, "delay", fail)
    with pytest.raises(HTTPException) as error:
        asyncio.run(libraries.scan_library(library.id, session, user))
    assert error.value.status_code == 503
    assert library.status == "failed"
    assert "secret-password" not in library.error
    assert session.commit.await_count == 2


@pytest.mark.parametrize("value,expected", [(b"99", True), (b"1", False), (b"101", False), (b"nan", False), (None, False)])
def test_readiness_rejects_stale_invalid_or_future_heartbeats(value, expected):
    assert readiness.heartbeat_is_fresh(value, now=100) is expected


def test_readiness_requires_scheduler_and_worker(monkeypatch):
    monkeypatch.setattr(health, "check_postgres", AsyncMock(return_value=True))
    monkeypatch.setattr(health, "check_redis", AsyncMock(return_value=True))
    monkeypatch.setattr(health, "background_readiness", AsyncMock(return_value={"worker": "ok", "scheduler": "unavailable"}))
    monkeypatch.setattr(health, "storage_readiness", lambda: {"imports": "ok"})
    response = Response()
    result = asyncio.run(health.ready(response))
    assert response.status_code == 503
    assert result["status"] == "not_ready"
    assert response.headers["cache-control"] == "no-store"


def test_first_backup_rejects_unapproved_account_before_queue(approved_storage):
    user = owner()
    user.email = "not-the-owner@example.test"
    with pytest.raises(HTTPException) as error:
        asyncio.run(storage_status.run_operational_backup(user))
    assert error.value.status_code == 403


@pytest.mark.parametrize("busy,publish_fails,expected", [(True, False, 409), (False, True, 503), (False, False, 202)])
def test_backup_queue_lock_and_redacted_failure(approved_storage, monkeypatch, busy, publish_fails, expected):
    client = SimpleNamespace(set=AsyncMock(return_value=not busy), eval=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(storage_status.Redis, "from_url", lambda *args, **kwargs: client)
    def publish(**kwargs):
        assert kwargs["retry"] is False
        if publish_fails:
            raise RuntimeError("postgres://private-credential")
        return SimpleNamespace(id="test-job")
    monkeypatch.setattr(storage_status.backup_operational_state_task, "apply_async", publish)
    if expected == 202:
        assert asyncio.run(storage_status.run_operational_backup(owner())) == {"status": "queued", "job_id": "test-job"}
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(storage_status.run_operational_backup(owner()))
        assert error.value.status_code == expected
        assert "private-credential" not in str(error.value.detail)
    assert client.eval.await_count == int(publish_fails and not busy)
    client.aclose.assert_awaited_once()


def test_duplicate_backup_delivery_cannot_run_twice(approved_storage, monkeypatch):
    import redis
    from app.worker import tasks

    class Client:
        value = "request"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def eval(self, script, count, key, expected, *remaining):
            if self.value != expected:
                return 0
            self.value = remaining[0] if remaining else None
            return 1

    client = Client()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: client)
    backup = AsyncMock()
    monkeypatch.setattr(tasks, "backup_operational_state", backup)
    tasks.backup_operational_state_task.run("request")
    tasks.backup_operational_state_task.run("request")
    backup.assert_awaited_once()


def test_setup_returns_only_friendly_selected_folder_name(approved_storage):
    manifest = json.loads(settings.storage_manifest_path.read_text())
    manifest["import_display_name"] = "Family photos"
    settings.storage_manifest_path.write_text(json.dumps(manifest))
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    result = asyncio.run(libraries.library_setup(session, owner()))
    assert result.folder_name == "Family photos"
    assert result.can_connect and result.storage_available
