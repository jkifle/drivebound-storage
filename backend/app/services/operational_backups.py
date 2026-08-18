"""Low-cost operational backups with verifiable logical PostgreSQL archives."""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.observability import metrics
from app.models.storage_policy import BackupArchive
from app.services.storage import sha256_file, validated_backup_path


def _safe_configuration_state() -> dict[str, object]:
    # Never copy credentials or encryption material into an operational backup.
    return {
        "deployment_mode": settings.deployment_mode,
        "paths": {
            "originals": str(settings.originals_path),
            "derivatives": str(settings.derivatives_path),
            "replica_roots": [str(path) for path in settings.replica_root_list],
            "backups": str(settings.backups_path),
        },
        "lifecycle": {
            "retention_days": settings.lifecycle_retention_days,
            "database_backup_retention_days": settings.database_backup_retention_days,
        },
        "encryption": {"enabled": settings.media_encryption_enabled, "format": "per-user-aes-256-gcm"},
    }


def _safe_configuration_document() -> dict[str, object]:
    return {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "configuration": _safe_configuration_state(),
    }


def _same_configuration_snapshot(path: Path) -> bool:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document.get("schema") == 1 and document.get("configuration") == _safe_configuration_state()
    except (OSError, ValueError, TypeError):
        return False


async def create_configuration_backup(session: AsyncSession) -> BackupArchive:
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    latest = await session.scalar(
        select(BackupArchive).where(BackupArchive.kind == "configuration").order_by(BackupArchive.created_at.desc()).limit(1)
    )
    # Configuration backups are incremental: an unchanged document keeps the
    # prior verified archive rather than creating identical files every day.
    still_within_retention = bool(
        latest
        and latest.created_at >= datetime.now(timezone.utc) - timedelta(days=settings.database_backup_retention_days)
    )
    if (
        latest is not None
        and still_within_retention
        and latest.status == "created"
        and latest.verification_status == "verified"
    ):
        try:
            latest_path = validated_backup_path(latest.path)
        except Exception:
            latest_path = Path()
        if (
            latest_path.is_file()
            and sha256_file(latest_path) == latest.checksum
            and _same_configuration_snapshot(latest_path)
        ):
            metrics.operations.observe_backup("configuration", "reused")
            metrics.operations.observe_backup("configuration", "verified")
            return latest
    document = _safe_configuration_document()
    encoded = json.dumps(document, sort_keys=True, indent=2).encode("utf-8")
    checksum = __import__("hashlib").sha256(encoded).hexdigest()
    destination = settings.backups_path / (
        f"configuration-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.json"
    )
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(destination)
    archive = BackupArchive(
        kind="configuration", path=str(destination), checksum=checksum, size_bytes=destination.stat().st_size,
        status="created", verification_status="verified", verified_at=datetime.now(timezone.utc),
        verification_detail="Checksum-verified JSON configuration snapshot.",
    )
    session.add(archive)
    await session.flush()
    metrics.operations.observe_backup("configuration", "created")
    metrics.operations.observe_backup("configuration", "verified")
    return archive


def _postgres_connection() -> tuple[list[str], dict[str, str]]:
    """Build pg_dump flags without exposing the password in the process list."""
    url = make_url(settings.database_url)
    command: list[str] = []
    if url.host:
        command.extend(["--host", url.host])
    if url.port:
        command.extend(["--port", str(url.port)])
    if url.username:
        command.extend(["--username", url.username])
    if url.database:
        command.extend(["--dbname", url.database])
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    if sslmode := url.query.get("sslmode"):
        environment["PGSSLMODE"] = str(sslmode)
    return command, environment


def _create_database_dump(destination: Path) -> None:
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise RuntimeError("pg_dump is not installed in the worker image")
    connection, environment = _postgres_connection()
    subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", "--no-privileges", "--file", str(destination), *connection],
        check=True, capture_output=True, text=True, timeout=60 * 60, env=environment,
    )


async def create_database_backup(session: AsyncSession, *, force: bool = False) -> BackupArchive | None:
    if not settings.database_backup_enabled:
        metrics.operations.observe_backup("database", "skipped")
        return None
    latest = await session.scalar(
        select(BackupArchive).where(BackupArchive.kind == "database").order_by(BackupArchive.created_at.desc()).limit(1)
    )
    if (
        not force
        and latest
        and latest.status == "created"
        and latest.verification_status in {"pending", "verified"}
        and latest.created_at + timedelta(hours=settings.database_backup_interval_hours) > datetime.now(timezone.utc)
    ):
        metrics.operations.observe_backup("database", "reused")
        if latest.verification_status == "verified":
            verified_at = latest.verified_at or latest.created_at
            if verified_at is not None:
                normalized = verified_at if verified_at.tzinfo else verified_at.replace(tzinfo=timezone.utc)
                metrics.operations.set_backup_last_verified("database", normalized.timestamp())
        return latest
    settings.backups_path.mkdir(parents=True, exist_ok=True)
    destination = settings.backups_path / f"database-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.dump"
    try:
        await asyncio.to_thread(_create_database_dump, destination)
        archive = BackupArchive(
            kind="database", path=str(destination), checksum=await asyncio.to_thread(sha256_file, destination),
            size_bytes=destination.stat().st_size, status="created", verification_status="pending",
        )
    except Exception as exc:
        destination.unlink(missing_ok=True)
        archive = BackupArchive(
            kind="database", path=str(destination), checksum="0" * 64, size_bytes=0,
            status="failed", verification_status="failed", verification_detail=str(exc)[:2000],
        )
    session.add(archive)
    await session.flush()
    metrics.operations.observe_backup("database", "created" if archive.status == "created" else "failed")
    return archive


def _verify_database_dump(path: Path) -> str:
    pg_restore = shutil.which("pg_restore")
    if pg_restore is None:
        raise RuntimeError("pg_restore is not installed in the worker image")
    result = subprocess.run([pg_restore, "--list", str(path)], check=True, capture_output=True, text=True, timeout=600)
    return f"Logical archive is readable ({len(result.stdout.splitlines())} objects listed)."


def _verify_configuration_snapshot(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != 1 or not isinstance(document.get("configuration"), dict):
        raise ValueError("Configuration snapshot has an unsupported format")
    encoded = json.dumps(document).lower()
    if any(name in encoded for name in ("jwt_secret", "smtp_password", "master_key", "database_url", "redis_url")):
        raise ValueError("Configuration snapshot contains a forbidden secret field")
    return "Checksum-verified, secret-free configuration snapshot."


async def verify_operational_backups(session: AsyncSession, archive_id: uuid.UUID | None = None) -> int:
    statement = select(BackupArchive).where(
        BackupArchive.status == "created", BackupArchive.verification_status != "verified"
    )
    if archive_id is not None:
        statement = statement.where(BackupArchive.id == archive_id)
    archives = list((await session.scalars(statement)).all())
    verified = 0
    for archive in archives:
        try:
            path = validated_backup_path(archive.path)
            if not path.is_file() or await asyncio.to_thread(sha256_file, path) != archive.checksum:
                raise ValueError("Archive checksum does not match")
            if archive.kind == "database":
                detail = await asyncio.to_thread(_verify_database_dump, path)
            elif archive.kind == "configuration":
                detail = await asyncio.to_thread(_verify_configuration_snapshot, path)
            else:
                raise ValueError("Unsupported backup archive kind")
            archive.verification_status = "verified"
            archive.verified_at = datetime.now(timezone.utc)
            archive.verification_detail = detail
            verified += 1
            metrics.operations.observe_backup(archive.kind, "verified")
        except Exception as exc:
            archive.verification_status = "failed"
            archive.verification_detail = str(exc)[:2000]
            metrics.operations.observe_backup(archive.kind, "failed")
    return verified


async def prune_operational_backups(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.database_backup_retention_days)
    archives = list((await session.scalars(select(BackupArchive).where(BackupArchive.created_at < cutoff))).all())
    removed = 0
    for archive in archives:
        try:
            validated_backup_path(archive.path).unlink(missing_ok=True)
        except Exception:
            continue
        await session.delete(archive)
        removed += 1
        metrics.operations.observe_backup(archive.kind, "pruned")
    return removed
