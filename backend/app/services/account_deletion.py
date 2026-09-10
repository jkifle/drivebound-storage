"""Crash-resumable account deletion and restore-suppression primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.account_deletion import AccountDeletionJob
from app.models.album import Album, AlbumInvite, AlbumMember
from app.models.asset import Asset
from app.models.auth import (
    AccountToken,
    AuditEvent,
    AuthSession,
    ExternalIdentity,
    MfaRecoveryCode,
    PasskeyCredential,
    WebAuthnChallenge,
)
from app.models.device import Device
from app.models.node import NodePairingCode, PairedNode
from app.models.replica import AssetReplica
from app.models.share import ShareLink
from app.models.upload_session import UploadSession
from app.models.user import User
from app.services.lifecycle import safely_purge_asset, safely_unlink_catalog_path
from app.services.host_storage import require_storage, require_storage_path


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _suppression_key() -> bytes:
    # JWT signing keys are routinely rotated.  The media master key is the
    # stable, file-backed recovery root required to read any historical media
    # archive, so restore suppression deliberately shares its lifecycle.
    return hashlib.sha256(settings.media_encryption_key + b":account-deletion:v1").digest()


def suppression_fingerprint(subject_id: uuid.UUID) -> str:
    """Non-reversible stable lookup used to suppress pre-deletion restores."""
    return hmac.new(_suppression_key(), subject_id.bytes, hashlib.sha256).hexdigest()


def _marker_tag(document: dict[str, object]) -> str:
    authenticated = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_suppression_key(), authenticated, hashlib.sha256).hexdigest()


def suppression_marker_path(job_id: uuid.UUID) -> Path:
    return settings.account_deletion_ledger_path / f"{job_id}.json"


def ensure_suppression_ledger_ready(*, require_existing: bool | None = None) -> Path:
    """Return a usable ledger directory, failing closed for public installs.

    Local development may initialize its own directory. Remote, staging, and
    production deployments must pre-provision the independently persisted
    mount; silently creating it on the application filesystem after a restore
    would make an empty/missing ledger indistinguishable from no deletions.
    """
    require_storage("backups")
    directory = settings.account_deletion_ledger_path
    required = (
        settings.remote_access_enabled or settings.production_like
        if require_existing is None
        else require_existing
    )
    if directory.exists():
        if not directory.is_dir():
            raise RuntimeError("Account-deletion suppression ledger path is not a directory")
    elif required:
        raise RuntimeError("Account-deletion suppression ledger directory is missing")
    else:
        directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(directory, 0o700)
    return directory


def write_suppression_marker(job_id: uuid.UUID, subject_id: uuid.UUID, requested_at: datetime) -> None:
    """Durably publish an independent, pseudonymous deletion-intent marker.

    The marker is written before the PostgreSQL commit and is authoritative.
    If the process stops between these operations, the periodic reconciler
    recreates the deletion job.  This privacy-biased ordering avoids a window
    in which a confirmed deletion can be resurrected from an older pg_dump.
    """
    directory = ensure_suppression_ledger_ready()
    destination = suppression_marker_path(job_id)
    document = {
        "schema": 1,
        "job_id": str(job_id),
        "subject_fingerprint": suppression_fingerprint(subject_id),
        "requested_at": _utc(requested_at).isoformat(),
    }
    document["authentication_tag"] = _marker_tag(document)
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise RuntimeError("Account-deletion suppression marker conflicts with durable evidence")
        return
    temporary = directory / f".{job_id}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def read_suppression_markers() -> list[dict[str, object]]:
    directory = settings.account_deletion_ledger_path
    if not directory.exists():
        if settings.remote_access_enabled or settings.production_like:
            raise RuntimeError("Account-deletion suppression ledger directory is missing")
        return []
    if not directory.is_dir():
        raise RuntimeError("Account-deletion suppression ledger path is not a directory")
    records: list[dict[str, object]] = []
    try:
        marker_paths = sorted(directory.glob("*.json"))
    except OSError as exc:
        raise RuntimeError("Account-deletion suppression ledger cannot be read") from exc
    seen_subjects: dict[str, str] = {}
    for path in marker_paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            uuid.UUID(str(document["job_id"]))
            datetime.fromisoformat(str(document["requested_at"]))
            fingerprint = str(document["subject_fingerprint"])
            supplied_tag = str(document.pop("authentication_tag"))
            if (
                document.get("schema") != 1
                or len(fingerprint) != 64
                or not hmac.compare_digest(supplied_tag, _marker_tag(document))
            ):
                raise ValueError
            document["authentication_tag"] = supplied_tag
            previous_job = seen_subjects.setdefault(fingerprint, str(document["job_id"]))
            if previous_job != str(document["job_id"]):
                raise ValueError
            records.append(document)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            # Invalid suppression evidence is a fail-closed operational error;
            # silently skipping it could resurrect a deleted account.
            raise RuntimeError("Account-deletion suppression ledger is invalid") from exc
    return records


def suppression_subject_is_denied(subject_id: uuid.UUID) -> bool:
    """Consult independently durable deletion intent before granting access.

    This deliberately reads the shared ledger on every authentication. It is
    the fail-closed bridge for the marker-fsynced/DB-commit-uncertain window;
    database credentials and public capabilities must not remain usable while
    reconciliation is waiting for PostgreSQL to recover.
    """
    fingerprint = suppression_fingerprint(subject_id)
    return any(
        hmac.compare_digest(str(record["subject_fingerprint"]), fingerprint)
        for record in read_suppression_markers()
    )


async def prepare_account_deletion(
    session: AsyncSession,
    user: User,
    *,
    requested_at: datetime | None = None,
    job_id: uuid.UUID | None = None,
) -> AccountDeletionJob:
    """Stage irreversible lockout and a durable cleanup job in one transaction.

    The caller must hold ``FOR UPDATE`` on ``user``.  Physical references stay
    in their catalog rows until the worker has removed each owned path.
    """
    existing = await session.scalar(
        select(AccountDeletionJob).where(AccountDeletionJob.subject_id == user.id).with_for_update()
    )
    if existing is not None:
        return existing
    now = _utc(requested_at or datetime.now(timezone.utc))
    original_email = user.email
    job = AccountDeletionJob(
        id=job_id or uuid.uuid4(),
        subject_id=user.id,
        status="pending",
        requested_at=now,
        not_before=now + timedelta(seconds=settings.account_deletion_quiescence_seconds),
        next_attempt_at=now,
        # This is an operator review date, not automatic expiry.  The external
        # marker remains until verified inventory proves no older archive or
        # off-site copy can be restored.
        suppression_review_after=now + timedelta(
            days=settings.database_backup_retention_days,
            hours=settings.database_backup_interval_hours + 1,
        ),
    )
    session.add(job)

    # Revoke public/collaboration capabilities as well as login credentials.
    await session.execute(delete(ShareLink).where(ShareLink.user_id == user.id))
    # Owned collaborative containers are capabilities in their own right.
    # Removing an Album only cascades membership/association rows, never Asset.
    await session.execute(delete(Album).where(Album.user_id == user.id))
    await session.execute(
        delete(AlbumInvite).where(or_(AlbumInvite.invited_by == user.id, func.lower(AlbumInvite.email) == original_email.lower()))
    )
    await session.execute(
        # Remove the deleting subject's access, but preserve other users who
        # were invited into third-party albums by this subject. Their
        # attribution becomes NULL through the 0017 SET NULL foreign key.
        delete(AlbumMember).where(AlbumMember.user_id == user.id)
    )
    for model in (
        AccountToken,
        WebAuthnChallenge,
        PasskeyCredential,
        MfaRecoveryCode,
        ExternalIdentity,
        Device,
        NodePairingCode,
        PairedNode,
        AuthSession,
    ):
        await session.execute(delete(model).where(model.user_id == user.id))

    # Preserve event type/timestamp evidence, but erase network fingerprints
    # and arbitrary detail that can contain filenames, checksums, or IDs.
    await session.execute(
        update(AuditEvent)
        .where(AuditEvent.user_id == user.id)
        .values(ip_address=None, user_agent=None, detail=None)
    )
    session.add(AuditEvent(
        user_id=user.id,
        event_type="account_deletion_requested",
        detail={"job_id": str(job.id), "credentials_destroyed": True},
    ))

    # Immediate irreversible lockout/crypto-erasure in the live database.
    user.disabled_at = now
    user.email = f"deleted-{user.id.hex}@invalid.drivebound"
    user.password_hash = f"!deleted!{secrets.token_hex(48)}"
    user.password_enabled = False
    user.password_set_at = None
    user.display_name = None
    user.onboarding_completed_at = None
    user.email_verified_at = None
    user.totp_secret_encrypted = None
    user.totp_enabled_at = None
    user.totp_last_used_step = None
    user.pending_totp_secret_encrypted = None
    user.pending_totp_expires_at = None
    user.pending_totp_session_id = None
    user.media_key_encrypted = None
    user.media_key_version = int(user.media_key_version or 0) + 1

    await session.execute(
        update(Asset)
        .where(Asset.user_id == user.id)
        .values(
            lifecycle_state="purging",
            trashed_at=func.coalesce(Asset.trashed_at, now),
            purge_after=now,
            deleted_by="account-deletion",
        )
    )
    await session.flush()
    return job


async def reconcile_suppression_ledger(session: AsyncSession) -> list[uuid.UUID]:
    """Recreate deletion intent after crash or restoration of an older dump."""
    markers = read_suppression_markers()
    existing_ids = set((await session.scalars(select(AccountDeletionJob.id))).all())
    missing = [record for record in markers if uuid.UUID(str(record["job_id"])) not in existing_ids]
    if not missing:
        return []
    users_by_fingerprint = {
        suppression_fingerprint(candidate.id): candidate
        for candidate in (await session.scalars(select(User))).all()
    }
    created: list[uuid.UUID] = []
    for record in missing:
        fingerprint = str(record["subject_fingerprint"])
        user = users_by_fingerprint.get(fingerprint)
        if user is None:
            continue
        locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
        if locked is None:
            continue
        job_uuid = uuid.UUID(str(record["job_id"]))
        requested_at = datetime.fromisoformat(str(record["requested_at"]))
        await prepare_account_deletion(session, locked, requested_at=requested_at, job_id=job_uuid)
        created.append(job_uuid)
    return created


def retry_delay(attempt: int) -> int:
    return min(
        settings.account_deletion_retry_base_seconds * (2 ** min(max(attempt - 1, 0), 10)),
        settings.account_deletion_retry_max_seconds,
    )


async def claim_account_deletion_job(session: AsyncSession, job_id: uuid.UUID) -> tuple[AccountDeletionJob, uuid.UUID] | None:
    now = datetime.now(timezone.utc)
    job = await session.scalar(select(AccountDeletionJob).where(AccountDeletionJob.id == job_id).with_for_update())
    if (
        job is None
        or job.status not in {"pending", "queued", "retry", "running"}
        or _utc(job.not_before) > now
        or (job.status != "queued" and _utc(job.next_attempt_at) > now)
    ):
        return None
    if job.status == "running" and job.lease_expires_at is not None and _utc(job.lease_expires_at) > now:
        return None
    token = uuid.uuid4()
    job.status = "running"
    job.lease_token = token
    job.lease_expires_at = now + timedelta(seconds=settings.account_deletion_lease_seconds)
    job.attempt_count += 1
    job.last_error_code = None
    await session.commit()
    return job, token


async def _owned_staging_paths_for_asset(asset_id: uuid.UUID, user_id: uuid.UUID) -> list[Path]:
    root = settings.staging_path.resolve()
    account_root = root / str(user_id)
    if not account_root.is_dir():
        return []
    return [candidate for candidate in account_root.glob(f"{asset_id}.*") if candidate.is_file()]


async def _canonical_live_paths(session: AsyncSession) -> set[str]:
    """Canonical identities of every remaining catalog path, including aliases."""
    from app.services.storage import canonical_storage_path

    values: list[str] = []
    for column in (Asset.original_path, Asset.thumbnail_path, Asset.preview_path):
        values.extend(item for item in (await session.scalars(select(column).where(column.is_not(None)))).all() if item)
    for column in (AssetReplica.path, AssetReplica.metadata_path):
        values.extend(item for item in (await session.scalars(select(column).where(column.is_not(None)))).all() if item)
    values.extend((await session.scalars(select(UploadSession.staging_path))).all())
    return {canonical_storage_path(value) for value in values if not str(value).startswith("duplicate:")}


def _is_link_or_reparse(path: Path) -> bool:
    """Reject POSIX links plus Windows junction/reparse traversal."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        # A disappearing or unreadable path is never safe to traverse during
        # terminal account cleanup; leave the job for explicit review.
        return True


async def sweep_account_namespaces(session: AsyncSession, subject_id: uuid.UUID) -> int:
    """Collect unreferenced crash orphans only inside account-owned namespaces.

    This intentionally never scans global/legacy checksum namespaces. It does
    not recursively delete directories: every file is path-validated, fenced,
    and checked against canonical aliases from every catalog path column.
    """
    from app.services.storage import canonical_storage_path

    require_storage("originals", "derivatives", "staging", "replicas", "backups")
    protected = await _canonical_live_paths(session)
    namespaces: list[tuple[Path, Path, bool]] = []
    retained = 0
    originals_root = settings.originals_path.resolve()
    namespaces.append((originals_root / str(subject_id), originals_root, False))
    derivatives_root = settings.derivatives_path.resolve()
    if derivatives_root.is_dir():
        for kind in derivatives_root.iterdir():
            if kind.is_dir():
                if _is_link_or_reparse(kind):
                    retained += 1
                else:
                    namespaces.append((kind / str(subject_id), derivatives_root, False))
    staging_root = settings.staging_path.resolve()
    namespaces.append((staging_root / str(subject_id), staging_root, False))
    for root in settings.replica_root_list:
        resolved_root = root.resolve()
        namespaces.append((resolved_root / str(subject_id), resolved_root, True))

    for namespace, root, replica_root in namespaces:
        require_storage_path(namespace)
        if not namespace.exists():
            continue
        if not namespace.is_dir():
            retained += 1
            continue
        expected = root / namespace.relative_to(root)
        lexical_namespace = Path(os.path.normcase(os.path.abspath(str(expected))))
        resolved_namespace = Path(os.path.normcase(str(namespace.resolve(strict=False))))
        if (
            namespace != expected
            or _is_link_or_reparse(namespace)
            or resolved_namespace != lexical_namespace
        ):
            retained += 1
            continue
        candidates = sorted(namespace.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for candidate in candidates:
            resolved_candidate = Path(os.path.normcase(str(candidate.resolve(strict=False))))
            if _is_link_or_reparse(candidate) or not resolved_candidate.is_relative_to(lexical_namespace):
                retained += 1
                continue
            if candidate.is_file():
                canonical = canonical_storage_path(candidate)
                if canonical in protected:
                    retained += 1
                    continue
                removed = await safely_unlink_catalog_path(
                    session,
                    canonical,
                    root=root,
                    replica_root=replica_root,
                )
                if not removed:
                    retained += 1
            elif candidate.is_dir():
                require_storage_path(candidate)
                try:
                    candidate.rmdir()
                except OSError:
                    pass
        try:
            require_storage_path(namespace)
            namespace.rmdir()
        except OSError:
            pass
    return retained


async def process_account_deletion_batch(
    session: AsyncSession,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> str:
    """Process bounded items with a commit after every durable unlink/delete."""
    for _ in range(settings.account_deletion_batch_size):
        job = await session.scalar(
            select(AccountDeletionJob)
            .where(AccountDeletionJob.id == job_id, AccountDeletionJob.lease_token == lease_token)
            .with_for_update()
        )
        if job is None or job.subject_id is None:
            await session.rollback()
            return "stale"
        user_id = job.subject_id
        upload = await session.scalar(
            select(UploadSession)
            .where(UploadSession.user_id == user_id)
            .order_by(UploadSession.created_at, UploadSession.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        try:
            require_storage("originals", "derivatives", "staging", "replicas", "backups")
            if upload is not None:
                if not upload.staging_path.startswith("duplicate:"):
                    await safely_unlink_catalog_path(
                        session,
                        upload.staging_path,
                        root=settings.staging_path,
                        excluding_upload_id=upload.id,
                    )
                await session.delete(upload)
                job.uploads_purged += 1
                job.failure_count = 0
                job.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=settings.account_deletion_lease_seconds
                )
                await session.commit()
                continue

            asset = await session.scalar(
                select(Asset)
                .where(Asset.user_id == user_id)
                .order_by(Asset.created_at, Asset.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if asset is not None:
                for staged in await _owned_staging_paths_for_asset(asset.id, asset.user_id):
                    await safely_unlink_catalog_path(session, str(staged.resolve()), root=settings.staging_path)
                retained: list[str] = []
                if not await safely_purge_asset(session, asset, retained_noncanonical=retained):
                    if retained:
                        await session.rollback()
                        job = await session.scalar(
                            select(AccountDeletionJob)
                            .where(
                                AccountDeletionJob.id == job_id,
                                AccountDeletionJob.status == "running",
                                AccountDeletionJob.lease_token == lease_token,
                            )
                            .with_for_update()
                        )
                        if job is None:
                            await session.rollback()
                            return "stale"
                        job.status = "manual_review"
                        job.retained_legacy_paths += len(retained)
                        job.lease_token = None
                        job.lease_expires_at = None
                        job.last_error_code = "noncanonical_legacy_path"
                        job.last_error_at = datetime.now(timezone.utc)
                        await session.commit()
                        return "manual_review"
                    raise RuntimeError("asset_cleanup_deferred")
                job.assets_purged += 1
                job.failure_count = 0
                job.retained_legacy_paths += len(retained)
                job.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=settings.account_deletion_lease_seconds
                )
                await session.commit()
                continue

            # User-first finalization prevents an upload/worker from publishing
            # a new catalog row after the zero-count check.
            # Re-establish the global lock order (User -> deletion job) before
            # the final zero-count check.  The batch started with the job lock,
            # so release it first to avoid deadlocking a concurrent, already-
            # authenticated idempotent deletion request.
            await session.rollback()
            user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
            job = await session.scalar(
                select(AccountDeletionJob)
                .where(AccountDeletionJob.id == job_id, AccountDeletionJob.lease_token == lease_token)
                .with_for_update()
            )
            if job is None:
                await session.rollback()
                return "stale"
            remaining_assets = await session.scalar(
                select(func.count()).select_from(Asset).where(Asset.user_id == user_id)
            )
            remaining_uploads = await session.scalar(
                select(func.count()).select_from(UploadSession).where(UploadSession.user_id == user_id)
            )
            if remaining_assets or remaining_uploads:
                await session.rollback()
                continue
            retained_orphans = await sweep_account_namespaces(session, user_id)
            if retained_orphans:
                job.status = "manual_review"
                job.retained_legacy_paths += retained_orphans
                job.lease_token = None
                job.lease_expires_at = None
                job.last_error_code = "account_namespace_requires_review"
                job.last_error_at = datetime.now(timezone.utc)
                await session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.user_id == user_id)
                    .values(ip_address=None, user_agent=None, detail=None)
                )
                await session.commit()
                return "manual_review"
            # Purge workers add path/checksum-oriented audit detail after the
            # request-time redaction. Scrub it again before the FK becomes NULL
            # and only the aggregate completion event remains.
            await session.execute(
                update(AuditEvent)
                .where(AuditEvent.user_id == user_id)
                .values(ip_address=None, user_agent=None, detail=None)
            )
            if user is not None:
                await session.delete(user)
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            job.subject_id = None
            job.lease_token = None
            job.lease_expires_at = None
            job.next_attempt_at = job.completed_at
            session.add(AuditEvent(
                user_id=None,
                event_type="account_deletion_completed",
                detail={
                    "job_id": str(job.id),
                    "assets_purged": job.assets_purged,
                    "uploads_purged": job.uploads_purged,
                },
            ))
            await session.commit()
            return "completed"
        except Exception as exc:
            await session.rollback()
            job = await session.scalar(
                select(AccountDeletionJob)
                .where(
                    AccountDeletionJob.id == job_id,
                    AccountDeletionJob.status == "running",
                    AccountDeletionJob.lease_token == lease_token,
                )
                .with_for_update()
            )
            if job is not None and job.status != "completed":
                now = datetime.now(timezone.utc)
                job.status = "retry"
                job.lease_token = None
                job.lease_expires_at = None
                job.last_error_code = str(exc)[:64] if str(exc) in {"asset_cleanup_deferred"} else type(exc).__name__[:64]
                job.last_error_at = now
                job.failure_count = int(job.failure_count or 0) + 1
                job.next_attempt_at = now + timedelta(seconds=retry_delay(job.failure_count))
                await session.commit()
                return "retry"
            await session.rollback()
            return "stale"

    job = await session.scalar(select(AccountDeletionJob).where(AccountDeletionJob.id == job_id).with_for_update())
    if job is not None and job.status == "running" and job.lease_token == lease_token:
        job.status = "pending"
        job.lease_token = None
        job.lease_expires_at = None
        job.next_attempt_at = datetime.now(timezone.utc)
        await session.commit()
    return "pending"


async def due_account_deletion_job_ids(session: AsyncSession, *, limit: int = 100) -> list[uuid.UUID]:
    now = datetime.now(timezone.utc)
    return list((await session.scalars(
        select(AccountDeletionJob.id)
        .where(
            AccountDeletionJob.status.in_(["pending", "queued", "retry", "running"]),
            AccountDeletionJob.not_before <= now,
            AccountDeletionJob.next_attempt_at <= now,
            or_(
                AccountDeletionJob.status != "running",
                AccountDeletionJob.lease_expires_at.is_(None),
                AccountDeletionJob.lease_expires_at <= now,
            ),
        )
        .order_by(AccountDeletionJob.next_attempt_at, AccountDeletionJob.requested_at)
        .limit(limit)
    )).all())


async def record_account_deletion_publish(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    published: bool,
) -> None:
    """Advance outbox state without overwriting a worker that already ran."""
    now = datetime.now(timezone.utc)
    job = await session.scalar(select(AccountDeletionJob).where(AccountDeletionJob.id == job_id).with_for_update())
    if job is None or job.status not in {"pending", "queued", "retry"}:
        return
    job.lease_token = None
    job.lease_expires_at = None
    if published:
        job.status = "queued"
        # A lost broker message is republished after this bounded visibility
        # window. Duplicate deliveries are safe because worker claiming is
        # leased and every unlink is idempotent.
        job.next_attempt_at = now + timedelta(seconds=settings.account_deletion_lease_seconds)
        job.last_error_code = None
    else:
        job.status = "retry"
        job.failure_count = int(job.failure_count or 0) + 1
        job.last_error_code = "broker_publish_failed"
        job.last_error_at = now
        job.next_attempt_at = now + timedelta(seconds=retry_delay(job.failure_count))
    await session.commit()
