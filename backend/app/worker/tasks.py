import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Iterator

from PIL import ExifTags, Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import func, select
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.observability import metrics
from app.models.asset import Asset
from app.models.external_library import ExternalLibrary
from app.models.replica import AssetReplica
from app.models.monitoring import MonitoringEvent
from app.models.storage_policy import StorageDrive
from app.models.user import User
from app.services.ingestion import encrypted_plaintext_checksum, initialize_user_media_key, user_media_key
from app.services.encryption import encrypt_file
from app.services.intelligence import embed_text, extract_ocr
from app.services.notifications import notify_user_devices
from app.services.storage import (
    canonical_storage_path,
    commit_encrypted_derivative,
    decrypted_temporary_file,
    derivative_path_for,
    lock_storage_path,
    original_path_for,
    sha256_file,
    validated_external_path,
    validated_replica_drive_path,
    validated_storage_path,
)
from app.services.replication import ensure_asset_replicas
from app.services.replication import rebalance_replicas
from app.services.media_groups import group_asset, perceptual_hash
from app.services.lifecycle import purge_due_assets, safely_unlink_catalog_path
from app.services.account_deletion import (
    claim_account_deletion_job,
    due_account_deletion_job_ids,
    process_account_deletion_batch,
    reconcile_suppression_ledger,
    record_account_deletion_publish,
)
from app.services.operational_backups import (
    create_configuration_backup,
    create_database_backup,
    prune_operational_backups,
    verify_operational_backups,
)
from app.worker.celery_app import celery_app


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip("\x00 ") or None
    return str(value).strip("\x00 ") or None


async def active_asset_for_write(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> tuple[Asset, User] | None:
    """Fence stale workers with the global User -> Asset lock order."""
    user_id = await session.scalar(select(Asset.user_id).where(Asset.id == asset_id))
    if user_id is None:
        return None
    user = await session.scalar(
        select(User).where(User.id == user_id, User.disabled_at.is_(None)).with_for_update(read=True)
    )
    if user is None:
        return None
    asset = await session.scalar(
        select(Asset)
        .where(Asset.id == asset_id, Asset.user_id == user.id, Asset.lifecycle_state == "active")
        .with_for_update()
    )
    return (asset, user) if asset is not None else None


def _decimal(values: object, reference: object) -> float | None:
    try:
        parts = [float(Fraction(value)) for value in values]  # type: ignore[arg-type]
        result = parts[0] + parts[1] / 60 + parts[2] / 3600
        if _text(reference) in {"S", "W"}:
            result = -result
        return result
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _taken_at(exif: dict[str, object], fallback: datetime) -> datetime:
    raw = _text(exif.get("DateTimeOriginal") or exif.get("DateTimeDigitized") or exif.get("DateTime"))
    if raw:
        try:
            parsed = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
            offset = _text(exif.get("OffsetTimeOriginal") or exif.get("OffsetTime"))
            if offset:
                sign = 1 if offset[0] == "+" else -1
                hours, minutes = (int(part) for part in offset[1:].split(":"))
                parsed = parsed.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))
            else:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, IndexError):
            pass
    return fallback


def _json_value(value: object) -> object:
    """Convert EXIF values to lossless, JSON-safe display metadata."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    try:
        fraction = Fraction(value)  # type: ignore[arg-type]
        return {"numerator": fraction.numerator, "denominator": fraction.denominator}
    except (TypeError, ValueError, ZeroDivisionError):
        return str(value)


def extract_and_thumbnail(original: Path, thumbnail: Path, fallback: datetime) -> dict[str, object]:
    with Image.open(original) as image:
        raw_exif = image.getexif()
        exif = {ExifTags.TAGS.get(key, str(key)): value for key, value in raw_exif.items()}
        if ExifTags.IFD.Exif in raw_exif:
            exif.update({
                ExifTags.TAGS.get(key, str(key)): value
                for key, value in raw_exif.get_ifd(ExifTags.IFD.Exif).items()
            })
        gps_raw = raw_exif.get_ifd(ExifTags.IFD.GPSInfo) if ExifTags.IFD.GPSInfo in raw_exif else {}
        gps = {ExifTags.GPSTAGS.get(key, str(key)): value for key, value in gps_raw.items()}

        corrected = ImageOps.exif_transpose(image)
        width, height = corrected.size
        image_hash = perceptual_hash(corrected)
        corrected.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        if corrected.mode not in {"RGB", "RGBA"}:
            corrected = corrected.convert("RGB")
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        corrected.save(thumbnail, "WEBP", quality=82, method=6)

        return {
            "width": width,
            "height": height,
            "perceptual_hash": image_hash,
            "taken_at": _taken_at(exif, fallback),
            "latitude": _decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef")),
            "longitude": _decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef")),
            "camera_make": _text(exif.get("Make")),
            "camera_model": _text(exif.get("Model")),
            "lens_model": _text(exif.get("LensModel")),
            "orientation": int(exif["Orientation"]) if exif.get("Orientation") is not None else None,
            "metadata_json": {
                "exif": {key: _json_value(value) for key, value in exif.items()},
                "gps": {key: _json_value(value) for key, value in gps.items()},
            },
            "thumbnail_path": str(thumbnail),
        }


def _media_datetime(value: object, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return fallback


def video_metadata_from_probe(probe: dict[str, object], fallback: datetime) -> dict[str, object]:
    streams = probe.get("streams") if isinstance(probe.get("streams"), list) else []
    video = next((item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"), {})
    format_data = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    tags: dict[str, object] = {}
    if isinstance(format_data.get("tags"), dict):
        tags.update(format_data["tags"])
    if isinstance(video, dict) and isinstance(video.get("tags"), dict):
        tags.update(video["tags"])
    location = tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")
    latitude = longitude = None
    if location:
        match = re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", str(location))
        if match:
            latitude, longitude = float(match.group(1)), float(match.group(2))
    duration_value = video.get("duration") if isinstance(video, dict) else None
    duration_value = duration_value or format_data.get("duration")
    rotation = None
    if isinstance(video, dict):
        rotation = video.get("tags", {}).get("rotate") if isinstance(video.get("tags"), dict) else None
        side_data = video.get("side_data_list")
        if rotation is None and isinstance(side_data, list):
            rotation = next((item.get("rotation") for item in side_data if isinstance(item, dict) and item.get("rotation") is not None), None)
    return {
        "width": int(video["width"]) if isinstance(video, dict) and video.get("width") else None,
        "height": int(video["height"]) if isinstance(video, dict) and video.get("height") else None,
        "duration_seconds": float(duration_value) if duration_value else None,
        "taken_at": _media_datetime(tags.get("creation_time"), fallback),
        "latitude": latitude,
        "longitude": longitude,
        "camera_make": _text(tags.get("com.apple.quicktime.make") or tags.get("make")),
        "camera_model": _text(tags.get("com.apple.quicktime.model") or tags.get("model")),
        "orientation": int(float(rotation)) if rotation is not None else None,
        "metadata_json": {"ffprobe": _json_value(probe)},
    }


def extract_video_metadata(original: Path, fallback: datetime) -> dict[str, object]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(original)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return video_metadata_from_probe(json.loads(completed.stdout), fallback)


@contextmanager
def plaintext_asset_file(asset: Asset, user: User | None) -> Iterator[Path]:
    """Provide plaintext to a processor without leaving it in permanent storage."""
    source = Path(asset.original_path)
    if asset.encryption_version:
        if user is None:
            raise ValueError("Encrypted asset is missing its owning account")
        suffix = Path(asset.original_filename or "").suffix
        with decrypted_temporary_file(source, user_media_key(user), suffix, asset.user_id) as plaintext:
            yield plaintext
    else:
        yield source


async def process_asset(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            writable = await active_asset_for_write(session, asset_id)
            if writable is None:
                return
            asset, user = writable
            asset.processing_status = "processing"
            asset.processing_error = None

            try:
                with plaintext_asset_file(asset, user) as original:
                    if asset.mime_type.startswith("video/"):
                        metadata = await asyncio.to_thread(
                            extract_video_metadata,
                            original,
                            asset.file_modified_at or asset.created_at,
                        )
                        for field, value in metadata.items():
                            setattr(asset, field, value)
                        asset.processing_status = "complete"
                    elif not asset.mime_type.startswith("image/"):
                        asset.taken_at = asset.file_modified_at or asset.created_at
                        asset.processing_status = "complete"
                    else:
                        if asset.encryption_version:
                            if user is None:
                                raise ValueError("Encrypted asset is missing its owning account")
                            thumbnail_directory = settings.staging_path / str(asset.user_id)
                            thumbnail_directory.mkdir(parents=True, exist_ok=True)
                            thumbnail = (thumbnail_directory / f"{asset.id}.{uuid.uuid4().hex}.thumbnail.webp").resolve()
                        else:
                            thumbnail = (
                                settings.derivatives_path / "thumbnails" / asset.checksum[:2] / f"{asset.checksum}.webp"
                            ).resolve()
                            await lock_storage_path(session, thumbnail)
                        metadata = await asyncio.to_thread(
                            extract_and_thumbnail,
                            original,
                            thumbnail,
                            asset.file_modified_at or asset.created_at,
                        )
                        if not asset.encryption_version:
                            metadata["thumbnail_path"] = canonical_storage_path(thumbnail)
                        if asset.encryption_version:
                            encrypted_thumbnail = derivative_path_for("thumbnails", asset.checksum, asset.user_id).resolve()
                            await lock_storage_path(session, encrypted_thumbnail)
                            commit_encrypted_derivative(thumbnail, encrypted_thumbnail, user_media_key(user))
                            metadata["thumbnail_path"] = canonical_storage_path(encrypted_thumbnail)
                        for field, value in metadata.items():
                            setattr(asset, field, value)
                        asset.processing_status = "complete"
                await session.commit()
                try:
                    index_asset_task.delay(str(asset.id))
                except Exception:
                    asset.intelligence_status = "failed"
                    await session.commit()
                try:
                    group_asset_task.delay(str(asset.id))
                except Exception:
                    # Grouping is a private convenience index. It must never
                    # make an otherwise healthy upload fail.
                    pass
                if settings.auto_protect_uploads:
                    try:
                        protect_asset_task.delay(str(asset.id))
                    except Exception:
                        asset.protection_status = "failed"
                        await session.commit()
            except Exception as exc:
                await session.rollback()
                writable = await active_asset_for_write(session, asset_id)
                if writable is not None:
                    asset, _ = writable
                    asset.processing_status = "failed"
                    asset.processing_error = str(exc)[:2000]
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="process_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def process_asset_task(asset_id: str) -> None:
    asyncio.run(process_asset(uuid.UUID(asset_id)))


async def index_asset(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            writable = await active_asset_for_write(session, asset_id)
            if writable is None:
                return
            asset, user = writable
            asset.intelligence_status = "indexing"
            try:
                with plaintext_asset_file(asset, user) as original:
                    ocr_text = await asyncio.to_thread(extract_ocr, original, asset.mime_type)
                parts = [
                    asset.original_filename or "", asset.relative_path or "", asset.mime_type,
                    asset.camera_make or "", asset.camera_model or "", asset.lens_model or "",
                    asset.taken_at.isoformat() if asset.taken_at else "", ocr_text,
                ]
                semantic_text = " ".join(part for part in parts if part).strip()
                asset.ocr_text = ocr_text or None
                asset.semantic_text = semantic_text
                asset.embedding = await asyncio.to_thread(embed_text, semantic_text or "media")
                asset.intelligence_status = "complete"
                asset.indexed_at = datetime.now(timezone.utc)
                await session.commit()
            except Exception:
                await session.rollback()
                writable = await active_asset_for_write(session, asset_id)
                if writable is not None:
                    asset, _ = writable
                    asset.intelligence_status = "failed"
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="index_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def index_asset_task(asset_id: str) -> None:
    asyncio.run(index_asset(uuid.UUID(asset_id)))


async def build_media_groups(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            writable = await active_asset_for_write(session, asset_id)
            if writable is None:
                return
            asset, _ = writable
            await group_asset(session, asset)
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="group_media", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def group_asset_task(asset_id: str) -> None:
    asyncio.run(build_media_groups(uuid.UUID(asset_id)))


MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
}


async def scan_external_library(library_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            library = await session.get(ExternalLibrary, library_id)
            if library is None:
                return
            owner = await session.scalar(
                select(User).where(User.id == library.user_id, User.disabled_at.is_(None)).with_for_update(read=True)
            )
            if owner is None:
                return
            library.status = "scanning"
            library.error = None
            await session.commit()
            try:
                root = validated_external_path(library.path)
                if not root.is_dir():
                    raise FileNotFoundError(f"External library is unavailable: {root}")
                paths = (path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS)
                batch: list[tuple[Path, str, int, str, datetime | None, datetime]] = []
                discovered = 0

                async def save_batch() -> bool:
                    nonlocal batch
                    if not batch:
                        return True
                    owner = await session.scalar(
                        select(User)
                        .where(User.id == library.user_id, User.disabled_at.is_(None))
                        .with_for_update(read=True)
                    )
                    current_library = await session.scalar(
                        select(ExternalLibrary)
                        .where(ExternalLibrary.id == library_id, ExternalLibrary.user_id == library.user_id)
                        .with_for_update()
                    )
                    if owner is None or current_library is None:
                        await session.rollback()
                        batch = []
                        return False
                    checksums = [item[1] for item in batch]
                    existing = set(
                        (
                            await session.scalars(
                                select(Asset.checksum).where(
                                    Asset.user_id == library.user_id,
                                    Asset.checksum.in_(checksums),
                                )
                            )
                        ).all()
                    )
                    created: list[Asset] = []
                    for path, checksum, size, mime_type, file_created_at, file_modified_at in batch:
                        if checksum in existing:
                            continue
                        canonical_path = path.resolve()
                        # Publish external path references under the same object
                        # lock used by collectors. The original itself remains
                        # read-only and is never removed by account cleanup.
                        await lock_storage_path(session, canonical_path)
                        asset = Asset(
                            user_id=current_library.user_id,
                            original_path=canonical_storage_path(canonical_path),
                            checksum=checksum,
                            storage_checksum=checksum,
                            encryption_version=0,
                            file_size=size,
                            mime_type=mime_type,
                            processing_status="pending",
                            storage_source="external",
                            external_library_id=current_library.id,
                            original_filename=path.name,
                            relative_path=str(path.relative_to(root)),
                            file_created_at=file_created_at,
                            file_modified_at=file_modified_at,
                        )
                        session.add(asset)
                        created.append(asset)
                    await session.commit()
                    for asset in created:
                        process_asset_task.delay(str(asset.id))
                    batch = []
                    return True

                for path in paths:
                    checksum = await asyncio.to_thread(sha256_file, path)
                    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    stat = path.stat()
                    file_created_at = (
                        datetime.fromtimestamp(stat.st_birthtime, timezone.utc)
                        if hasattr(stat, "st_birthtime") else None
                    )
                    file_modified_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                    batch.append((path, checksum, stat.st_size, mime_type, file_created_at, file_modified_at))
                    discovered += 1
                    if len(batch) >= 100:
                        if not await save_batch():
                            return
                if not await save_batch():
                    return
                owner = await session.scalar(
                    select(User).where(User.id == library.user_id, User.disabled_at.is_(None)).with_for_update(read=True)
                )
                library = await session.scalar(
                    select(ExternalLibrary).where(ExternalLibrary.id == library_id).with_for_update()
                )
                if owner is not None and library is not None:
                    library.status = "idle"
                    library.file_count = discovered
                    library.last_scanned_at = datetime.now(timezone.utc)
                    await session.commit()
            except Exception as exc:
                await session.rollback()
                library = await session.get(ExternalLibrary, library_id)
                owner = await session.get(User, library.user_id) if library is not None else None
                if library is not None and owner is not None and owner.disabled_at is None:
                    library.status = "failed"
                    library.error = str(exc)[:2000]
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="scan_library", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_library_task(library_id: str) -> None:
    asyncio.run(scan_external_library(uuid.UUID(library_id)))


async def protect_asset(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            writable = await active_asset_for_write(session, asset_id)
            if writable is None:
                return
            asset, _ = writable
            asset.protection_status = "protecting"
            # Phase 2 selects the configured number of eligible drives and
            # verifies every copy before exposing the asset as protected.
            try:
                await ensure_asset_replicas(session, asset)
                await session.commit()
                return
            except Exception:
                await session.rollback()
                writable = await active_asset_for_write(session, asset_id)
                if writable is not None:
                    asset, _ = writable
                    asset.protection_status = "failed"
                    await session.commit()
                raise

            # Kept below temporarily for compatibility with a worker process
            # that imports old task code during a rolling deployment. The new
            # policy-driven branch above always returns before this legacy
            # single-drive implementation can run.
            source = (
                validated_external_path(asset.original_path)
                if asset.storage_source == "external"
                else validated_storage_path(asset.original_path, settings.originals_path)
            )
            destination = (
                settings.replica_path / str(asset.user_id) / asset.checksum[:2] / asset.checksum
                if asset.encryption_version
                else settings.replica_path / asset.checksum[:2] / asset.checksum
            )
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and await asyncio.to_thread(sha256_file, destination) != asset.storage_checksum:
                    destination.unlink()
                if not destination.exists():
                    temporary_copy = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.protect")
                    with source.open("rb") as input_file, temporary_copy.open("xb") as output_file:
                        shutil.copyfileobj(input_file, output_file, length=4 * 1024 * 1024)
                        output_file.flush()
                        os.fsync(output_file.fileno())
                    os.replace(temporary_copy, destination)
                replica_checksum = await asyncio.to_thread(sha256_file, destination)
                if replica_checksum != asset.storage_checksum:
                    raise ValueError("Replica checksum verification failed")
                metadata_path = destination.with_name(f"{destination.name}.metadata.json")
                metadata_document = {
                    "schema": 1,
                    "asset_id": str(asset.id),
                    "checksum": asset.checksum,
                    "storage_checksum": asset.storage_checksum,
                    "encryption_version": asset.encryption_version,
                    "original_filename": asset.original_filename,
                    "relative_path": asset.relative_path,
                    "mime_type": asset.mime_type,
                    "file_size": asset.file_size,
                    "taken_at": asset.taken_at.isoformat() if asset.taken_at else None,
                    "file_created_at": asset.file_created_at.isoformat() if asset.file_created_at else None,
                    "file_modified_at": asset.file_modified_at.isoformat() if asset.file_modified_at else None,
                    "latitude": asset.latitude,
                    "longitude": asset.longitude,
                    "camera_make": asset.camera_make,
                    "camera_model": asset.camera_model,
                    "lens_model": asset.lens_model,
                    "orientation": asset.orientation,
                    "embedded_metadata": asset.metadata_json,
                }
                temporary_metadata = metadata_path.with_suffix(".json.tmp")
                temporary_metadata.write_text(json.dumps(metadata_document, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temporary_metadata, metadata_path)
                stale_replica_path: Path | None = None
                stale_metadata_path: Path | None = None
                replica = await session.scalar(select(AssetReplica).where(AssetReplica.asset_id == asset.id))
                if replica is None:
                    session.add(
                        AssetReplica(
                            asset_id=asset.id,
                            path=str(destination),
                            checksum=replica_checksum,
                            metadata_path=str(metadata_path),
                            verified_at=datetime.now(timezone.utc),
                        )
                    )
                else:
                    previous_path = Path(replica.path)
                    previous_metadata_path = Path(replica.metadata_path) if replica.metadata_path else None
                    replica.status = "verified"
                    replica.path = str(destination)
                    replica.checksum = replica_checksum
                    replica.metadata_path = str(metadata_path)
                    replica.verified_at = datetime.now(timezone.utc)
                    # Legacy protection copies may be plaintext. Remove one
                    # only after the encrypted destination is checksum-verified
                    # and its database record now points to that destination.
                    if previous_path != destination:
                        stale_replica_path = previous_path
                        stale_metadata_path = previous_metadata_path
                asset.protection_status = "protected"
                await session.commit()
                if stale_replica_path:
                    try:
                        validated_storage_path(str(stale_replica_path), settings.replica_path).unlink(missing_ok=True)
                        if stale_metadata_path:
                            validated_storage_path(str(stale_metadata_path), settings.replica_path).unlink(missing_ok=True)
                    except Exception:
                        # The replacement is already sound; a later hygiene
                        # pass can remove an inaccessible stale file.
                        pass
            except Exception:
                await session.rollback()
                asset = await session.get(Asset, asset_id)
                if asset is not None:
                    asset.protection_status = "failed"
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="protect_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def protect_asset_task(asset_id: str) -> None:
    asyncio.run(protect_asset(uuid.UUID(asset_id)))


class InvalidReplicaError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReplicaCandidate:
    id: uuid.UUID
    path: Path


@dataclass(frozen=True)
class ReplicaFailure:
    id: uuid.UUID
    reason: str


class NoUsableReplicaError(RuntimeError):
    def __init__(self, failures: list[ReplicaFailure]):
        super().__init__("No checksum-valid protection copy is available")
        self.failures = failures


def _copy_verified_replica(
    source: Path,
    destination: Path,
    expected_checksum: str,
    timestamp: float | None,
) -> None:
    """Atomically publish one checksum-valid replica as the managed original."""
    if not source.is_file():
        raise InvalidReplicaError("missing")
    try:
        if sha256_file(source) != expected_checksum:
            raise InvalidReplicaError("checksum_mismatch")
    except FileNotFoundError as exc:
        raise InvalidReplicaError("missing") from exc
    except OSError as exc:
        raise InvalidReplicaError("unreadable") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restore")
    try:
        try:
            with source.open("rb") as input_file, temporary.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=4 * 1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
        except FileNotFoundError as exc:
            raise InvalidReplicaError("disappeared") from exc
        if sha256_file(temporary) != expected_checksum:
            raise InvalidReplicaError("copy_checksum_mismatch")
        os.replace(temporary, destination)
        if timestamp is not None:
            os.utime(destination, (timestamp, timestamp))
    finally:
        temporary.unlink(missing_ok=True)


def restore_from_candidates(
    candidates: list[ReplicaCandidate],
    destination: Path,
    expected_checksum: str,
    timestamp: float | None = None,
) -> tuple[uuid.UUID, list[ReplicaFailure]]:
    """Try candidates in caller-defined order and stop at the first valid copy."""
    failures: list[ReplicaFailure] = []
    for candidate in candidates:
        try:
            _copy_verified_replica(candidate.path, destination, expected_checksum, timestamp)
            return candidate.id, failures
        except InvalidReplicaError as exc:
            failures.append(ReplicaFailure(candidate.id, exc.code))
    raise NoUsableReplicaError(failures)


def _filesystem_timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.timestamp()


async def _open_recovery_event(
    session: AsyncSession,
    asset: Asset,
    *,
    kind: str,
    severity: str,
    message: str,
    detail: dict[str, object] | None = None,
) -> MonitoringEvent:
    existing = await session.scalar(select(MonitoringEvent).where(
        MonitoringEvent.asset_id == asset.id,
        MonitoringEvent.kind == kind,
        MonitoringEvent.status == "open",
    ))
    if existing is not None:
        existing.detail = detail
        return existing
    event = MonitoringEvent(
        user_id=asset.user_id,
        asset_id=asset.id,
        kind=kind,
        severity=severity,
        message=message,
        detail=detail,
    )
    session.add(event)
    return event


async def restore_asset(asset_id: uuid.UUID) -> bool:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    metric_recorded = False
    try:
        async with session_factory() as session:
            writable = await active_asset_for_write(session, asset_id)
            if writable is None:
                metrics.operations.observe_recovery("asset_restore", "skipped")
                metric_recorded = True
                return False
            asset, _ = writable
            replicas = list((await session.scalars(
                select(AssetReplica)
                .outerjoin(StorageDrive, StorageDrive.id == AssetReplica.drive_id)
                .where(
                    AssetReplica.asset_id == asset.id,
                    AssetReplica.status == "verified",
                    AssetReplica.verification_status == "verified",
                )
                .order_by(
                    func.coalesce(StorageDrive.priority, 100).desc(),
                    AssetReplica.verified_at.desc(),
                    AssetReplica.id.asc(),
                )
            )).all())
            asset.restore_status = "restoring"

            candidates: list[ReplicaCandidate] = []
            failures: list[ReplicaFailure] = []
            by_id = {replica.id: replica for replica in replicas}
            for replica in replicas:
                try:
                    candidates.append(ReplicaCandidate(replica.id, validated_replica_drive_path(replica.path)))
                except Exception:
                    failures.append(ReplicaFailure(replica.id, "invalid_path"))

            destination = original_path_for(
                asset.checksum,
                asset.original_filename,
                asset.user_id if asset.encryption_version else None,
            ).resolve()
            await lock_storage_path(session, destination)
            timestamp_value = asset.file_modified_at or asset.file_created_at or asset.taken_at
            try:
                selected_id, attempted_failures = await asyncio.to_thread(
                    restore_from_candidates,
                    candidates,
                    destination,
                    asset.storage_checksum,
                    _filesystem_timestamp(timestamp_value),
                )
                failures.extend(attempted_failures)
                for failure in failures:
                    replica = by_id[failure.id]
                    replica.status = "failed"
                    replica.verification_status = "failed"
                    replica.failure_count += 1
                    replica.last_error = f"Restore candidate failed: {failure.reason}"
                selected = by_id[selected_id]
                selected.status = "verified"
                selected.verification_status = "verified"
                selected.failure_count = 0
                selected.last_error = None
                selected.verified_at = datetime.now(timezone.utc)
                asset.original_path = canonical_storage_path(destination)
                asset.storage_source = "managed"
                asset.external_library_id = None
                asset.restore_status = "restored"
                asset.restored_at = datetime.now(timezone.utc)
                if failures:
                    asset.protection_status = "degraded"
                open_events = list((await session.scalars(select(MonitoringEvent).where(
                    MonitoringEvent.asset_id == asset.id,
                    MonitoringEvent.status == "open",
                    MonitoringEvent.kind.in_([
                        "automatic_restore",
                        "original_missing_recovery_available",
                        "original_unrecoverable",
                        "restore_write_failed",
                    ]),
                ))).all())
                for event in open_events:
                    event.status = "resolved"
                    event.resolved_at = datetime.now(timezone.utc)
                await session.commit()
                await notify_user_devices(
                    session,
                    asset.user_id,
                    title="Storage recovery completed",
                    body="Drivebound restored the original from a verified protection copy.",
                    data={"kind": "storage_recovered", "asset_id": str(asset.id)},
                )
                metrics.operations.observe_recovery("asset_restore", "success")
                metric_recorded = True
                return True
            except NoUsableReplicaError as exc:
                failures.extend(exc.failures)
                for failure in failures:
                    replica = by_id.get(failure.id)
                    if replica is not None:
                        replica.status = "failed"
                        replica.verification_status = "failed"
                        replica.failure_count += 1
                        replica.last_error = f"Restore candidate failed: {failure.reason}"
                asset.restore_status = "failed"
                asset.protection_status = "failed"
                await _open_recovery_event(
                    session,
                    asset,
                    kind="original_unrecoverable",
                    severity="critical",
                    message="The original is unavailable and every verified protection copy failed validation.",
                    detail={
                        "attempted_replicas": len(replicas),
                        "failed_replica_ids": [str(failure.id) for failure in failures],
                    },
                )
                await notify_user_devices(
                    session,
                    asset.user_id,
                    title="Storage recovery needs attention",
                    body="Drivebound could not find a valid protection copy for one original.",
                    data={"kind": "storage_unrecoverable", "asset_id": str(asset.id)},
                )
                await session.commit()
                metrics.operations.observe_recovery("asset_restore", "unrecoverable")
                metric_recorded = True
                return False
            except Exception as exc:
                asset.restore_status = "failed"
                await _open_recovery_event(
                    session,
                    asset,
                    kind="restore_write_failed",
                    severity="warning",
                    message="A verified copy was found, but the managed original could not be written.",
                    detail={"reason": type(exc).__name__},
                )
                await session.commit()
                raise
    except Exception:
        if not metric_recorded:
            metrics.operations.observe_recovery("asset_restore", "failed")
        raise
    finally:
        await engine.dispose()


@celery_app.task(name="restore_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def restore_asset_task(asset_id: str) -> bool:
    return asyncio.run(restore_asset(uuid.UUID(asset_id)))


async def monitor_storage(user_id: uuid.UUID | None = None, *, repair: bool = True) -> None:
    """Verify originals and protection copies and queue safe, automatic recovery."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    mode = "scheduled_repair" if user_id is None else ("verify_and_repair" if repair else "verify_only")
    results: Counter[str] = Counter()
    try:
        async with session_factory() as session:
            asset_statement = select(Asset).where(Asset.lifecycle_state == "active")
            if user_id is not None:
                asset_statement = asset_statement.where(Asset.user_id == user_id)
            assets = (await session.scalars(asset_statement)).all()
            for asset in assets:
                original = Path(asset.original_path)
                replicas = list((await session.scalars(
                    select(AssetReplica).where(AssetReplica.asset_id == asset.id)
                )).all())
                original_ok = original.is_file() and await asyncio.to_thread(sha256_file, original) == asset.storage_checksum
                healthy_replicas = []
                for replica in replicas:
                    try:
                        replica_path = validated_replica_drive_path(replica.path)
                        healthy = replica_path.is_file() and await asyncio.to_thread(sha256_file, replica_path) == asset.storage_checksum
                        if healthy:
                            healthy_replicas.append(replica)
                            replica.status = "verified"
                            replica.verification_status = "verified"
                            replica.failure_count = 0
                            replica.last_error = None
                            replica.verified_at = datetime.now(timezone.utc)
                        else:
                            replica.status = "failed"
                            replica.verification_status = "failed"
                            replica.failure_count += 1
                            replica.last_error = "Replica is missing or checksum verification failed"
                    except Exception as exc:
                        replica.status = "failed"
                        replica.verification_status = "failed"
                        replica.failure_count += 1
                        replica.last_error = str(exc)[:1000]
                if original_ok:
                    results["healthy" if healthy_replicas else "replica_degraded"] += 1
                elif healthy_replicas:
                    results["recoverable"] += 1
                else:
                    results["unrecoverable"] += 1
                if not original_ok and healthy_replicas and asset.storage_source == "managed":
                    if repair:
                        already_open = await session.scalar(select(MonitoringEvent).where(
                            MonitoringEvent.asset_id == asset.id,
                            MonitoringEvent.kind == "automatic_restore",
                            MonitoringEvent.status == "open",
                        ))
                        if already_open is None:
                            session.add(MonitoringEvent(
                                user_id=asset.user_id, asset_id=asset.id, kind="automatic_restore",
                                severity="warning", message="Original was missing; restore queued from verified protection copies.",
                            ))
                            await notify_user_devices(
                                session,
                                asset.user_id,
                                title="Storage recovery started",
                                body="Drivebound is restoring a missing original from a verified protection copy.",
                                data={"kind": "storage_warning", "asset_id": str(asset.id)},
                            )
                            asset.restore_status = "queued"
                            restore_asset_task.delay(str(asset.id))
                            metrics.operations.observe_recovery("asset_restore", "queued")
                    else:
                        await _open_recovery_event(
                            session,
                            asset,
                            kind="original_missing_recovery_available",
                            severity="warning",
                            message="The managed original is missing, but a checksum-valid protection copy is available.",
                            detail={"healthy_replicas": len(healthy_replicas)},
                        )
                elif not original_ok and not healthy_replicas:
                    asset.restore_status = "failed"
                    asset.protection_status = "failed"
                    await _open_recovery_event(
                        session,
                        asset,
                        kind="original_unrecoverable",
                        severity="critical",
                        message="The original is unavailable and no checksum-valid protection copy is available.",
                        detail={"attempted_replicas": len(replicas)},
                    )
                elif repair and original_ok and settings.auto_protect_uploads and (len(healthy_replicas) < 1 or asset.protection_status != "protected"):
                    asset.protection_status = "pending"
                    protect_asset_task.delay(str(asset.id))
                elif original_ok and healthy_replicas:
                    events = (await session.scalars(select(MonitoringEvent).where(
                        MonitoringEvent.asset_id == asset.id,
                        MonitoringEvent.status == "open",
                        MonitoringEvent.kind.in_([
                            "automatic_restore",
                            "original_missing_recovery_available",
                            "original_unrecoverable",
                            "restore_write_failed",
                            "replica_verification_failed",
                        ]),
                    ))).all()
                    for event in events:
                        event.status = "resolved"
                        event.resolved_at = datetime.now(timezone.utc)
            await session.commit()
        metrics.operations.observe_storage_run(mode, "success", results)
    except Exception:
        metrics.operations.observe_storage_run(mode, "failure")
        raise
    finally:
        await engine.dispose()


async def finish_account_verification(
    user_id: uuid.UUID,
    verification_id: uuid.UUID,
    outcome: str,
) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            verification = await session.scalar(
                select(MonitoringEvent)
                .where(
                    MonitoringEvent.id == verification_id,
                    MonitoringEvent.user_id == user_id,
                    MonitoringEvent.kind == "account_storage_verification",
                    MonitoringEvent.status == "open",
                )
                .order_by(MonitoringEvent.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if verification is None:
                return
            verification.status = "resolved" if outcome == "completed" else "failed"
            verification.resolved_at = datetime.now(timezone.utc)
            verification.message = (
                "Account storage verification completed."
                if outcome == "completed"
                else "Account storage verification failed before completion."
            )
            verification.detail = {**(verification.detail or {}), "outcome": outcome}
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="monitor_storage")
def monitor_storage_task(
    user_id: str | None = None,
    repair: bool = True,
    verification_id: str | None = None,
) -> None:
    parsed_user_id = uuid.UUID(user_id) if user_id else None
    parsed_verification_id = uuid.UUID(verification_id) if verification_id else None
    try:
        asyncio.run(monitor_storage(parsed_user_id, repair=repair))
    except Exception:
        metrics.operations.observe_recovery("storage_verification", "failed")
        if parsed_user_id is not None and parsed_verification_id is not None:
            try:
                asyncio.run(finish_account_verification(parsed_user_id, parsed_verification_id, "failed"))
            except Exception:
                pass
        raise
    metrics.operations.observe_recovery("storage_verification", "success")
    if parsed_user_id is not None and parsed_verification_id is not None:
        asyncio.run(finish_account_verification(parsed_user_id, parsed_verification_id, "completed"))


async def rebalance_user_storage(user_id: uuid.UUID) -> int:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id, User.disabled_at.is_(None)).with_for_update(read=True)
            )
            if user is None:
                return 0
            moved = await rebalance_replicas(session, user_id)
            await session.commit()
            return moved
    finally:
        await engine.dispose()


@celery_app.task(name="rebalance_user_storage", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def rebalance_user_storage_task(user_id: str) -> int:
    return asyncio.run(rebalance_user_storage(uuid.UUID(user_id)))


async def purge_expired_assets(asset_id: uuid.UUID | None = None) -> int:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await purge_due_assets(session, asset_id=asset_id)
    finally:
        await engine.dispose()


@celery_app.task(name="purge_expired_assets")
def purge_expired_assets_task(asset_id: str | None = None) -> int:
    return asyncio.run(purge_expired_assets(uuid.UUID(asset_id) if asset_id else None))


async def cleanup_account_deletion(job_id: uuid.UUID) -> str:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            claimed = await claim_account_deletion_job(session, job_id)
            if claimed is None:
                return "skipped"
            _, lease_token = claimed
            return await process_account_deletion_batch(session, job_id, lease_token)
    finally:
        await engine.dispose()


@celery_app.task(name="cleanup_account_deletion")
def cleanup_account_deletion_task(job_id: str) -> str:
    # Retries are represented durably in AccountDeletionJob rather than in the
    # transient broker result backend. The dispatcher recovers failed publish,
    # worker crashes, and expired leases.
    outcome = asyncio.run(cleanup_account_deletion(uuid.UUID(job_id)))
    if outcome == "retry":
        # Retry timing already lives in PostgreSQL. Raising here makes worker
        # failure metrics/alerts truthful without asking Celery to duplicate
        # that retry state in Redis.
        raise RuntimeError("durable account deletion cleanup deferred")
    if outcome == "manual_review":
        # The durable state is terminal until an operator resolves retained
        # catalog evidence. Surface this delivery as a worker failure so the
        # existing bounded task alerting cannot mistake it for completion.
        raise RuntimeError("durable account deletion requires manual review")
    return outcome


async def dispatch_account_deletions() -> int:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    published = 0
    try:
        async with session_factory() as session:
            await reconcile_suppression_ledger(session)
            await session.commit()
            due = await due_account_deletion_job_ids(session)
            for job_id in due:
                try:
                    cleanup_account_deletion_task.delay(str(job_id))
                except Exception:
                    await record_account_deletion_publish(session, job_id, published=False)
                else:
                    await record_account_deletion_publish(session, job_id, published=True)
                    published += 1
        return published
    finally:
        await engine.dispose()


@celery_app.task(name="dispatch_account_deletions")
def dispatch_account_deletions_task() -> int:
    return asyncio.run(dispatch_account_deletions())


async def backup_operational_state() -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await create_configuration_backup(session)
            await create_database_backup(session)
            await verify_operational_backups(session)
            await prune_operational_backups(session)
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="backup_operational_state")
def backup_operational_state_task() -> None:
    asyncio.run(backup_operational_state())


async def migrate_legacy_media(limit: int = 10) -> int:
    """Move a bounded batch of legacy managed media into per-user encryption.

    This is deliberately opt-in through ``MEDIA_ENCRYPTION_MIGRATE_LEGACY``.
    It leaves a plaintext shared object in place until no legacy asset still
    references it, and re-queues an encrypted protection copy afterwards.
    """
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    migrated = 0
    try:
        async with session_factory() as session:
            assets = list((await session.scalars(
                select(Asset)
                .where(
                    Asset.encryption_version == 0,
                    Asset.storage_source == "managed",
                    Asset.lifecycle_state == "active",
                )
                .order_by(Asset.created_at)
                .limit(limit)
            )).all())
            for candidate in assets:
                user = await session.scalar(
                    select(User)
                    .where(User.id == candidate.user_id, User.disabled_at.is_(None))
                    .with_for_update()
                )
                if user is None:
                    continue
                asset = await session.scalar(
                    select(Asset)
                    .where(
                        Asset.id == candidate.id,
                        Asset.user_id == user.id,
                        Asset.lifecycle_state == "active",
                        Asset.encryption_version == 0,
                    )
                    .with_for_update()
                )
                if asset is None:
                    await session.rollback()
                    continue
                if user.media_key_encrypted is None:
                    # Make the DEK durable before publishing any ciphertext.
                    # A retry can authenticate an already-linked destination
                    # only with this committed key.
                    initialize_user_media_key(user)
                    await session.commit()
                    user = await session.scalar(
                        select(User)
                        .where(User.id == candidate.user_id, User.disabled_at.is_(None))
                        .with_for_update()
                    )
                    if user is None:
                        continue
                    asset = await session.scalar(
                        select(Asset)
                        .where(
                            Asset.id == candidate.id,
                            Asset.user_id == user.id,
                            Asset.lifecycle_state == "active",
                            Asset.encryption_version == 0,
                        )
                        .with_for_update()
                    )
                    if asset is None:
                        await session.rollback()
                        continue
                source = validated_storage_path(asset.original_path, settings.originals_path)
                if not source.is_file():
                    await session.rollback()
                    continue
                destination = original_path_for(asset.checksum, asset.original_filename, asset.user_id).resolve()
                try:
                    await lock_storage_path(session, destination)
                    key = user_media_key(user)
                    if not destination.exists():
                        await asyncio.to_thread(encrypt_file, source, destination, key)
                    elif await asyncio.to_thread(
                        encrypted_plaintext_checksum, destination, key, asset.file_size
                    ) != asset.checksum:
                        raise ValueError("Existing encrypted migration destination does not authenticate")
                    encrypted_thumbnail: Path | None = None
                    old_thumbnail = Path(asset.thumbnail_path) if asset.thumbnail_path else None
                    if old_thumbnail and old_thumbnail.is_file():
                        encrypted_thumbnail = derivative_path_for("thumbnails", asset.checksum, asset.user_id).resolve()
                        await lock_storage_path(session, encrypted_thumbnail)
                        if not encrypted_thumbnail.exists():
                            await asyncio.to_thread(encrypt_file, old_thumbnail, encrypted_thumbnail, key)
                        elif await asyncio.to_thread(
                            encrypted_plaintext_checksum, encrypted_thumbnail, key
                        ) != await asyncio.to_thread(sha256_file, old_thumbnail):
                            raise ValueError("Existing encrypted thumbnail does not authenticate")
                    remaining_originals = await session.scalar(
                        select(Asset.id)
                        .where(
                            Asset.original_path == asset.original_path,
                            Asset.encryption_version == 0,
                            Asset.id != asset.id,
                        )
                        .limit(1)
                    )
                    remaining_thumbnails = await session.scalar(
                        select(Asset.id)
                        .where(
                            Asset.thumbnail_path == asset.thumbnail_path,
                            Asset.encryption_version == 0,
                            Asset.id != asset.id,
                        )
                        .limit(1)
                    ) if old_thumbnail else None
                    asset.original_path = canonical_storage_path(destination)
                    asset.storage_checksum = await asyncio.to_thread(sha256_file, destination)
                    asset.encryption_version = 1
                    if encrypted_thumbnail:
                        asset.thumbnail_path = canonical_storage_path(encrypted_thumbnail)
                    asset.protection_status = "unprotected"
                    await session.commit()
                    # Reacquire the account fence before collecting legacy
                    # globals. If deletion won the race, its worker handles
                    # catalog-backed paths and legacy hygiene stays conservative.
                    active_user = await session.scalar(
                        select(User).where(User.id == user.id, User.disabled_at.is_(None)).with_for_update(read=True)
                    )
                    if active_user is not None and remaining_originals is None:
                        await safely_unlink_catalog_path(session, str(source.resolve()), root=settings.originals_path)
                    if active_user is not None and old_thumbnail and remaining_thumbnails is None:
                        await safely_unlink_catalog_path(
                            session, str(old_thumbnail.resolve()), root=settings.derivatives_path
                        )
                    await session.commit()
                    try:
                        protect_asset_task.delay(str(asset.id))
                    except Exception:
                        asset.protection_status = "failed"
                        await session.commit()
                    migrated += 1
                except Exception:
                    await session.rollback()
                    raise
    finally:
        await engine.dispose()
    return migrated


@celery_app.task(name="migrate_legacy_media", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def migrate_legacy_media_task(limit: int = 10) -> int:
    return asyncio.run(migrate_legacy_media(limit))
