import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Response
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1.assets import readable_asset
from app.api.v1.uploads import UploadPrincipal, create_upload
from app.api.v1.uploads import upload_principal
from app.core.security import create_access_token, current_auth
from app.main import runtime_configuration_errors
from app.models.account_deletion import AccountDeletionJob
from app.models.asset import Asset
from app.models.album import AlbumMember
from app.models.auth import AuditEvent, AuthSession
from app.models.user import User
from app.schemas.upload import UploadSessionCreate
from app.schemas.auth import DeleteAccountRequest
from app.services import account_deletion, storage
from app.services.account_deletion import (
    prepare_account_deletion,
    claim_account_deletion_job,
    process_account_deletion_batch,
    record_account_deletion_publish,
    suppression_fingerprint,
    sweep_account_namespaces,
    write_suppression_marker,
)
from app.services.ingestion import encrypted_plaintext_checksum, initialize_user_media_key, persist_managed_asset, user_media_key
from app.services.lifecycle import safely_unlink_catalog_path
from app.worker.tasks import active_asset_for_write


def run(awaitable):
    return asyncio.run(awaitable)


class EmptyScalars:
    def all(self):
        return []


class PathSession:
    def __init__(self, scalar_values=None):
        self.scalar_values = list(scalar_values or [])
        self.executed = []

    async def execute(self, statement, *_args, **_kwargs):
        self.executed.append(statement)

    async def scalar(self, _statement):
        return self.scalar_values.pop(0) if self.scalar_values else None

    async def scalars(self, _statement):
        return EmptyScalars()


def test_suppression_marker_is_pseudonymous_authenticated_and_jwt_rotation_safe(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(account_deletion.settings, "backups_path", tmp_path)
    subject_id = uuid.uuid4()
    job_id = uuid.uuid4()
    requested = datetime.now(timezone.utc)
    before = suppression_fingerprint(subject_id)

    write_suppression_marker(job_id, subject_id, requested)
    marker = json.loads((tmp_path / "account-deletion-suppressions" / f"{job_id}.json").read_text())
    monkeypatch.setattr(account_deletion.settings, "jwt_secret", "rotated-jwt-signing-key-that-does-not-affect-ledger")

    assert suppression_fingerprint(subject_id) == before
    assert str(subject_id) not in json.dumps(marker)
    assert len(marker["subject_fingerprint"]) == 64
    assert len(marker["authentication_tag"]) == 64
    assert account_deletion.read_suppression_markers()[0]["job_id"] == str(job_id)

    marker["requested_at"] = "2099-01-01T00:00:00+00:00"
    (tmp_path / "account-deletion-suppressions" / f"{job_id}.json").write_text(json.dumps(marker))
    with pytest.raises(RuntimeError, match="ledger is invalid"):
        account_deletion.read_suppression_markers()


def test_disabled_account_can_never_regenerate_a_destroyed_media_key() -> None:
    user = User(
        id=uuid.uuid4(),
        email="deleted@example.invalid",
        password_hash="!deleted!",
        password_enabled=False,
        disabled_at=datetime.now(timezone.utc),
        media_key_encrypted=None,
    )
    with pytest.raises(ValueError, match="disabled account"):
        user_media_key(user)
    assert user.media_key_encrypted is None


def test_missing_media_key_is_never_lazily_regenerated() -> None:
    user = User(
        id=uuid.uuid4(), email="person@example.com", password_hash="hash",
        password_enabled=True, media_key_encrypted=None,
    )
    with pytest.raises(ValueError, match="has not been initialized"):
        user_media_key(user)
    assert user.media_key_encrypted is None


class PrepareSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.executed = []
        self.added = []

    async def scalar(self, _statement):
        return self.existing

    async def execute(self, statement):
        self.executed.append(statement)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        return None


def test_prepare_is_idempotent_and_crypto_erases_every_capability() -> None:
    now = datetime.now(timezone.utc)
    user = User(
        id=uuid.uuid4(),
        email="person@example.com",
        password_hash="hash",
        password_enabled=True,
        display_name="Person",
        media_key_encrypted="wrapped-key",
        totp_secret_encrypted="wrapped-totp",
        totp_enabled_at=now,
    )
    session = PrepareSession()

    job = run(prepare_account_deletion(session, user, requested_at=now))

    affected = {statement.table.name for statement in session.executed}
    assert {
        "share_links",
        "albums",
        "album_invites",
        "album_members",
        "account_tokens",
        "webauthn_challenges",
        "passkey_credentials",
        "mfa_recovery_codes",
        "external_identities",
        "devices",
        "node_pairing_codes",
        "paired_nodes",
        "auth_sessions",
        "assets",
        "audit_events",
    } <= affected
    assert user.disabled_at == now
    assert user.media_key_encrypted is None
    assert user.password_enabled is False
    assert user.email.startswith("deleted-")
    assert job.subject_id == user.id

    existing_session = PrepareSession(existing=job)
    assert run(prepare_account_deletion(existing_session, user)) is job
    assert existing_session.executed == []
    membership_delete = next(statement for statement in session.executed if statement.table.name == "album_members")
    assert "invited_by" not in str(membership_delete)


def test_external_or_shared_path_is_never_unlinked(monkeypatch, tmp_path: Path) -> None:
    path = (tmp_path / "shared-object").resolve()
    path.write_bytes(b"keep")

    external_session = PathSession([uuid.uuid4()])
    assert run(safely_unlink_catalog_path(external_session, storage.canonical_storage_path(path), root=tmp_path)) is False
    assert path.exists()

    # No external reference, but another Asset path column still owns it.
    shared_session = PathSession([None, uuid.uuid4()])
    assert run(safely_unlink_catalog_path(shared_session, storage.canonical_storage_path(path), root=tmp_path)) is False
    assert path.exists()


def test_noncanonical_alias_is_retained_instead_of_unlinked(tmp_path: Path) -> None:
    directory = tmp_path / "objects"
    directory.mkdir()
    path = directory / "object"
    path.write_bytes(b"keep")
    alias = str(directory / ".." / "objects" / "object")

    with pytest.raises(ValueError, match="noncanonical_catalog_path"):
        run(safely_unlink_catalog_path(PathSession(), alias, root=tmp_path))
    assert path.exists()


def test_canonical_delete_is_blocked_by_foreign_external_alias(tmp_path: Path) -> None:
    directory = tmp_path / "objects"
    directory.mkdir()
    path = (directory / "object").resolve()
    path.write_bytes(b"keep")
    alias = str(directory / ".." / "objects" / "object")

    class AliasScalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class AliasSession(PathSession):
        async def scalars(self, _statement):
            # The first fallback scan is external Asset.original_path.
            return AliasScalars([alias])

    session = AliasSession([None, None, None, None])
    assert run(safely_unlink_catalog_path(
        session, storage.canonical_storage_path(path), root=tmp_path,
    )) is False
    assert path.exists()


def test_broker_failure_leaves_a_durable_due_retry() -> None:
    now = datetime.now(timezone.utc)
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=uuid.uuid4(), status="pending", requested_at=now,
        not_before=now, next_attempt_at=now, suppression_review_after=now + timedelta(days=31),
    )

    class PublishSession:
        committed = False

        async def scalar(self, _statement):
            return job

        async def commit(self):
            self.committed = True

    session = PublishSession()
    run(record_account_deletion_publish(session, job.id, published=False))

    assert session.committed
    assert job.status == "retry"
    assert job.last_error_code == "broker_publish_failed"
    assert job.next_attempt_at > now


def test_manual_review_job_cannot_be_reclaimed_or_overwritten_by_late_publish() -> None:
    now = datetime.now(timezone.utc)
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=uuid.uuid4(), status="manual_review",
        requested_at=now, not_before=now, next_attempt_at=now,
        suppression_review_after=now + timedelta(days=31), retained_legacy_paths=1,
    )

    class ManualSession:
        commits = 0

        async def scalar(self, _statement):
            return job

        async def commit(self):
            self.commits += 1

    session = ManualSession()
    assert run(claim_account_deletion_job(session, job.id)) is None
    run(record_account_deletion_publish(session, job.id, published=True))
    assert job.status == "manual_review"
    assert session.commits == 0


def test_stale_worker_cannot_clear_a_newer_cleanup_lease(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    token = uuid.uuid4()
    subject = uuid.uuid4()
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=subject, status="running", lease_token=token,
        requested_at=now, not_before=now, next_attempt_at=now,
        lease_expires_at=now + timedelta(minutes=1), suppression_review_after=now + timedelta(days=31),
    )
    asset = Asset(
        id=uuid.uuid4(), user_id=subject, logical_id=uuid.uuid4(), original_path="x",
        checksum="a" * 64, storage_checksum="b" * 64, file_size=1, mime_type="image/jpeg",
        lifecycle_state="purging", trashed_at=now, purge_after=now,
    )

    class LeaseSession:
        def __init__(self):
            self.values = [job, None, asset, None]
            self.commits = 0

        async def scalar(self, _statement):
            return self.values.pop(0)

        async def rollback(self):
            return None

        async def commit(self):
            self.commits += 1

    async def fail_purge(*_args, **_kwargs):
        raise OSError("drive unavailable")

    monkeypatch.setattr(account_deletion, "safely_purge_asset", fail_purge)
    monkeypatch.setattr(account_deletion, "_owned_staging_paths_for_asset", lambda *_ids: asyncio.sleep(0, result=[]))
    session = LeaseSession()

    assert run(process_account_deletion_batch(session, job.id, token)) == "stale"
    assert session.commits == 0


def test_orphan_sweep_removes_only_account_scoped_bytes(monkeypatch, tmp_path: Path) -> None:
    subject = uuid.uuid4()
    originals = tmp_path / "originals"
    derivatives = tmp_path / "derivatives"
    replicas = tmp_path / "replicas"
    for root in (originals, derivatives, replicas):
        root.mkdir()
    orphan = originals / str(subject) / "aa" / "object"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"orphan")
    global_byte = originals / "aa" / "shared"
    global_byte.parent.mkdir(parents=True)
    global_byte.write_bytes(b"global")
    monkeypatch.setattr(account_deletion.settings, "originals_path", originals)
    monkeypatch.setattr(account_deletion.settings, "derivatives_path", derivatives)
    monkeypatch.setattr(account_deletion.settings, "replica_roots", str(replicas))

    retained = run(sweep_account_namespaces(PathSession(), subject))

    assert retained == 0
    assert not orphan.exists()
    assert global_byte.exists()


def test_orphan_sweep_never_traverses_a_linked_account_namespace(monkeypatch, tmp_path: Path) -> None:
    subject = uuid.uuid4()
    originals = tmp_path / "originals"
    derivatives = tmp_path / "derivatives"
    replicas = tmp_path / "replicas"
    foreign = tmp_path / "external"
    for root in (originals, derivatives, replicas, foreign):
        root.mkdir()
    foreign_file = foreign / "foreign-original"
    foreign_file.write_bytes(b"keep")
    namespace = originals / str(subject)
    try:
        namespace.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("directory links/junctions are unavailable to this test user")
    monkeypatch.setattr(account_deletion.settings, "originals_path", originals)
    monkeypatch.setattr(account_deletion.settings, "derivatives_path", derivatives)
    monkeypatch.setattr(account_deletion.settings, "replica_roots", str(replicas))

    assert run(sweep_account_namespaces(PathSession(), subject)) >= 1
    assert foreign_file.exists()


def test_stale_asset_writer_skips_a_disabled_owner() -> None:
    class WriterSession:
        def __init__(self):
            self.values = [uuid.uuid4(), None]

        async def scalar(self, _statement):
            return self.values.pop(0)

    assert run(active_asset_for_write(WriterSession(), uuid.uuid4())) is None


def test_create_upload_commit_failure_removes_untracked_staging_file(monkeypatch, tmp_path: Path) -> None:
    from app.api.v1 import uploads

    owner = User(id=uuid.uuid4(), email="person@example.com", password_hash="hash", password_enabled=True)

    class FailingSession:
        async def scalar(self, _statement):
            return owner

        def add(self, _item):
            return None

        async def commit(self):
            raise RuntimeError("database unavailable")

        async def rollback(self):
            return None

    monkeypatch.setattr(uploads.settings, "staging_path", tmp_path)
    payload = UploadSessionCreate(filename="photo.jpg", mime_type="image/jpeg", total_size=10)

    with pytest.raises(RuntimeError, match="database unavailable"):
        run(create_upload(payload, FailingSession(), UploadPrincipal(owner.id)))

    assert list(tmp_path.rglob("*.resumable")) == []


def test_deletion_schema_has_no_user_fk_and_downgrade_is_guarded() -> None:
    assert not list(AccountDeletionJob.__table__.columns["subject_id"].foreign_keys)
    migration = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0017_durable_account_deletion.py"
    ).read_text(encoding="utf-8")
    assert "cannot downgrade 0017 while account deletion evidence exists" in migration
    assert 'down_revision = "0016"' in migration
    invited_by = AlbumMember.__table__.columns["invited_by"]
    assert invited_by.nullable
    assert next(iter(invited_by.foreign_keys)).ondelete == "SET NULL"
    assert 'ondelete="SET NULL"' in migration


def test_ledger_reconciliation_recreates_and_disables_a_restored_user(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(account_deletion.settings, "backups_path", tmp_path)
    subject = uuid.uuid4()
    job_id = uuid.uuid4()
    requested = datetime.now(timezone.utc) - timedelta(days=1)
    write_suppression_marker(job_id, subject, requested)
    user = User(
        id=subject, email="restored@example.com", password_hash="restored-hash",
        password_enabled=True, media_key_encrypted="restored-wrapped-key", media_key_version=1,
    )

    class SequenceScalars:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class ReconcileSession(PrepareSession):
        def __init__(self):
            super().__init__()
            self.scalar_values = [user, None]
            self.scalars_values = [[], [user]]

        async def scalars(self, _statement):
            return SequenceScalars(self.scalars_values.pop(0))

        async def scalar(self, _statement):
            return self.scalar_values.pop(0)

    session = ReconcileSession()
    created = run(account_deletion.reconcile_suppression_ledger(session))

    assert created == [job_id]
    assert user.disabled_at is not None
    assert user.media_key_encrypted is None
    assert user.password_enabled is False


def test_finalization_redacts_worker_audit_and_keeps_only_aggregate_completion(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    subject = uuid.uuid4()
    token = uuid.uuid4()
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=subject, status="running", lease_token=token,
        attempt_count=1, assets_purged=3, uploads_purged=2, retained_legacy_paths=0,
        requested_at=now, not_before=now, next_attempt_at=now,
        lease_expires_at=now + timedelta(minutes=1), suppression_review_after=now + timedelta(days=31),
    )
    user = User(
        id=subject, email=f"deleted-{subject.hex}@invalid.drivebound", password_hash="!deleted!",
        password_enabled=False, disabled_at=now,
    )

    class FinalSession:
        def __init__(self):
            self.values = [job, None, None, user, job, 0, 0]
            self.executed = []
            self.added = []
            self.deleted = []

        async def scalar(self, _statement):
            return self.values.pop(0)

        async def rollback(self):
            return None

        async def execute(self, statement):
            self.executed.append(statement)

        async def delete(self, item):
            self.deleted.append(item)

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            return None

    async def no_orphans(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(account_deletion, "sweep_account_namespaces", no_orphans)
    session = FinalSession()
    assert run(process_account_deletion_batch(session, job.id, token)) == "completed"

    audit_update = next(statement for statement in session.executed if statement.table.name == "audit_events")
    assert "detail" in {column.name for column in audit_update._values}
    completion = next(item for item in session.added if isinstance(item, AuditEvent))
    assert completion.event_type == "account_deletion_completed"
    assert completion.user_id is None
    assert completion.detail == {"job_id": str(job.id), "assets_purged": 3, "uploads_purged": 2}
    assert job.subject_id is None
    assert user in session.deleted


def test_authoritative_marker_commit_uncertainty_returns_accepted_and_clears_cookies(monkeypatch) -> None:
    from app.api.v1 import auth

    now = datetime.now(timezone.utc)
    user = User(
        id=uuid.uuid4(), email="person@example.com", password_hash="!passwordless!",
        password_enabled=False, media_key_version=1,
    )
    current = AuthSession(
        id=uuid.uuid4(), user_id=user.id, refresh_token_hash="a" * 64,
        expires_at=now + timedelta(days=1), reauthenticated_at=now,
    )
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=user.id, status="pending", requested_at=now,
        not_before=now, next_attempt_at=now, suppression_review_after=now + timedelta(days=31),
    )

    class UncertainSession:
        def __init__(self):
            self.values = [user, None]
            self.commits = 0

        async def scalar(self, _statement):
            return self.values.pop(0)

        async def commit(self):
            self.commits += 1
            raise RuntimeError("commit outcome unknown")

        async def rollback(self):
            raise RuntimeError("connection is poisoned")

    class FreshSession:
        async def commit(self):
            raise RuntimeError("database is still unavailable")

    class FreshContext:
        async def __aenter__(self):
            return FreshSession()

        async def __aexit__(self, *_args):
            return False

    def fresh_factory():
        return FreshContext()

    async def prepared(*_args, **_kwargs):
        return job

    async def reconciled(*_args, **_kwargs):
        return []

    monkeypatch.setattr(auth, "prepare_account_deletion", prepared)
    monkeypatch.setattr(auth, "write_suppression_marker", lambda *_args: None)
    monkeypatch.setattr(auth, "session_has_recent_auth", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(auth, "reconcile_suppression_ledger", reconciled)
    monkeypatch.setattr(auth, "async_session_factory", fresh_factory)
    request = Request({"type": "http", "method": "DELETE", "path": "/api/v1/auth/account", "headers": [], "client": ("127.0.0.1", 1)})
    response = Response()

    result = run(auth.delete_account(
        DeleteAccountRequest(confirmation="DELETE MY ACCOUNT"), request, response,
        UncertainSession(), (user, current),
    ))

    assert "durably accepted" in result.message
    assert "No account changes" not in result.message
    assert any(b"Max-Age=0" in value for key, value in response.raw_headers if key == b"set-cookie")


def test_retained_noncanonical_paths_require_manual_review_and_keep_catalog_evidence(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    subject = uuid.uuid4()
    token = uuid.uuid4()
    job = AccountDeletionJob(
        id=uuid.uuid4(), subject_id=subject, status="running", lease_token=token,
        attempt_count=1, assets_purged=0, uploads_purged=0, retained_legacy_paths=0,
        requested_at=now, not_before=now, next_attempt_at=now,
        lease_expires_at=now + timedelta(minutes=1), suppression_review_after=now + timedelta(days=31),
    )
    asset = Asset(
        id=uuid.uuid4(), user_id=subject, logical_id=uuid.uuid4(), original_path="legacy/../alias",
        checksum="a" * 64, storage_checksum="b" * 64, file_size=1, mime_type="image/jpeg",
        lifecycle_state="purging", trashed_at=now, purge_after=now,
    )

    class BatchSession:
        def __init__(self):
            self.values = [job, None, asset, job]
            self.deleted = []

        async def scalar(self, _statement):
            return self.values.pop(0)

        async def rollback(self):
            return None

        async def commit(self):
            return None

        async def delete(self, item):
            self.deleted.append(item)

    async def retained_purge(session, item, *, retained_noncanonical):
        retained_noncanonical.extend(["managed_original", "derivative"])
        return False

    async def no_staging(*_args, **_kwargs):
        return []

    monkeypatch.setattr(account_deletion.settings, "account_deletion_batch_size", 1)
    monkeypatch.setattr(account_deletion, "safely_purge_asset", retained_purge)
    monkeypatch.setattr(account_deletion, "_owned_staging_paths_for_asset", no_staging)
    session = BatchSession()

    assert run(process_account_deletion_batch(session, job.id, token)) == "manual_review"
    assert job.status == "manual_review"
    assert job.assets_purged == 0
    assert job.retained_legacy_paths == 2
    assert asset not in session.deleted
    assert job.subject_id == subject


def test_first_dek_is_durable_before_ciphertext_and_retry_authenticates_existing_bytes(
    monkeypatch, tmp_path: Path,
) -> None:
    from app.services import ingestion

    content = b"crash-safe encrypted upload"
    checksum = __import__("hashlib").sha256(content).hexdigest()
    user = User(
        id=uuid.uuid4(), email="person@example.com", password_hash="hash", password_enabled=True,
    )
    monkeypatch.setattr(ingestion.settings, "originals_path", tmp_path / "originals")
    monkeypatch.setattr(ingestion.settings, "media_encryption_enabled", True)

    class UploadSession:
        def __init__(self, *, fail_catalog_commit: bool):
            self.fail_catalog_commit = fail_catalog_commit
            self.commit_count = 0
            self.scalar_count = 0
            self.added = []

        async def scalar(self, _statement):
            self.scalar_count += 1
            # User fence, optional reacquire after key commit, then dedupe.
            if self.scalar_count == 1:
                return user
            if self.scalar_count == 2 and self.commit_count == 1:
                return user
            return None

        async def execute(self, *_args, **_kwargs):
            return None

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            self.commit_count += 1
            if self.fail_catalog_commit and self.commit_count == 2:
                raise RuntimeError("catalog commit failed after ciphertext publication")

        async def refresh(self, _item):
            return None

    first_stage = tmp_path / "first.upload"
    first_stage.write_bytes(content)
    first = UploadSession(fail_catalog_commit=True)
    with pytest.raises(RuntimeError, match="catalog commit failed"):
        run(persist_managed_asset(
            first, user_id=user.id, staged_path=first_stage, checksum=checksum,
            file_size=len(content), mime_type="image/jpeg", filename="photo.jpg",
        ))
    wrapped_key = user.media_key_encrypted
    assert wrapped_key is not None
    final_path = ingestion.original_path_for(checksum, "photo.jpg", user.id).resolve()
    assert final_path.is_file()

    second_stage = tmp_path / "second.upload"
    second_stage.write_bytes(content)
    second = UploadSession(fail_catalog_commit=False)
    asset, duplicate = run(persist_managed_asset(
        second, user_id=user.id, staged_path=second_stage, checksum=checksum,
        file_size=len(content), mime_type="image/jpeg", filename="photo.jpg",
    ))
    assert not duplicate
    assert user.media_key_encrypted == wrapped_key
    assert encrypted_plaintext_checksum(final_path, user_media_key(user)) == checksum
    assert asset.original_path == storage.canonical_storage_path(final_path)


def test_suppression_marker_denies_bearer_device_and_collaborator_reads(monkeypatch, tmp_path: Path) -> None:
    from app.models.device import Device

    monkeypatch.setattr(account_deletion.settings, "backups_path", tmp_path)
    subject = uuid.uuid4()
    marker_job = uuid.uuid4()
    write_suppression_marker(marker_job, subject, datetime.now(timezone.utc))
    session_id = uuid.uuid4()
    token = create_access_token(subject, session_id)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})

    class BearerSession:
        async def scalar(self, _statement):
            raise AssertionError("denylist must run before database credential lookup")

    with pytest.raises(HTTPException) as bearer_error:
        run(current_auth(request, token, BearerSession()))
    assert bearer_error.value.status_code == 401

    device = Device(id=uuid.uuid4(), user_id=subject, name="Phone", platform="android", token_hash="x")

    class DeviceSession:
        async def scalar(self, _statement):
            return device

    with pytest.raises(HTTPException) as device_error:
        run(upload_principal(request, "device-token", None, DeviceSession()))
    assert device_error.value.status_code == 401

    asset = Asset(
        id=uuid.uuid4(), user_id=subject, logical_id=uuid.uuid4(), original_path="external",
        checksum="a" * 64, storage_checksum="b" * 64, file_size=1, mime_type="image/jpeg",
        lifecycle_state="active",
    )

    class CollaboratorSession:
        async def scalar(self, _statement):
            return asset

    assert run(readable_asset(CollaboratorSession(), uuid.uuid4(), asset.id)) is None


def test_public_runtime_requires_ledger_mount_and_disjoint_external_roots(monkeypatch, tmp_path: Path) -> None:
    originals = tmp_path / "managed"
    monkeypatch.setattr(account_deletion.settings, "remote_access_enabled", True)
    monkeypatch.setattr(account_deletion.settings, "backups_path", tmp_path / "missing-backups")
    monkeypatch.setattr(account_deletion.settings, "originals_path", originals)
    monkeypatch.setattr(account_deletion.settings, "external_library_roots", str(originals / "imports"))

    errors = runtime_configuration_errors()
    assert "the independently persisted account-deletion suppression ledger directory is missing" in errors
    assert "EXTERNAL_LIBRARY_ROOTS must not overlap Drivebound-managed storage roots" in errors
    with pytest.raises(RuntimeError, match="ledger directory is missing"):
        account_deletion.read_suppression_markers()
