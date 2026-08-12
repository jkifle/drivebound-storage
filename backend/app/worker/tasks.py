import asyncio
import json
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Iterator

from PIL import ExifTags, Image, ImageOps
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.asset import Asset
from app.models.external_library import ExternalLibrary
from app.models.replica import AssetReplica
from app.models.monitoring import MonitoringEvent
from app.models.user import User
from app.services.ingestion import user_media_key
from app.services.encryption import encrypt_file
from app.services.intelligence import embed_text, extract_ocr
from app.services.notifications import notify_user_devices
from app.services.storage import (
    commit_encrypted_derivative,
    decrypted_temporary_file,
    derivative_path_for,
    original_path_for,
    sha256_file,
    validated_external_path,
    validated_storage_path,
)
from app.worker.celery_app import celery_app


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip("\x00 ") or None
    return str(value).strip("\x00 ") or None


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
        corrected.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
        if corrected.mode not in {"RGB", "RGBA"}:
            corrected = corrected.convert("RGB")
        thumbnail.parent.mkdir(parents=True, exist_ok=True)
        corrected.save(thumbnail, "WEBP", quality=82, method=6)

        return {
            "width": width,
            "height": height,
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
        with decrypted_temporary_file(source, user_media_key(user), suffix) as plaintext:
            yield plaintext
    else:
        yield source


async def process_asset(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return
            asset.processing_status = "processing"
            asset.processing_error = None
            await session.commit()

            try:
                user = await session.get(User, asset.user_id) if asset.encryption_version else None
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
                            thumbnail = settings.staging_path / f"{asset.id}.{uuid.uuid4().hex}.thumbnail.webp"
                        else:
                            thumbnail = settings.derivatives_path / "thumbnails" / asset.checksum[:2] / f"{asset.checksum}.webp"
                        metadata = await asyncio.to_thread(
                            extract_and_thumbnail,
                            original,
                            thumbnail,
                            asset.file_modified_at or asset.created_at,
                        )
                        if asset.encryption_version:
                            encrypted_thumbnail = derivative_path_for("thumbnails", asset.checksum, asset.user_id)
                            commit_encrypted_derivative(thumbnail, encrypted_thumbnail, user_media_key(user))
                            metadata["thumbnail_path"] = str(encrypted_thumbnail)
                        for field, value in metadata.items():
                            setattr(asset, field, value)
                        asset.processing_status = "complete"
                await session.commit()
                try:
                    index_asset_task.delay(str(asset.id))
                except Exception:
                    asset.intelligence_status = "failed"
                    await session.commit()
                if settings.auto_protect_uploads:
                    try:
                        protect_asset_task.delay(str(asset.id))
                    except Exception:
                        asset.protection_status = "failed"
                        await session.commit()
            except Exception as exc:
                await session.rollback()
                asset = await session.get(Asset, asset_id)
                if asset is not None:
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
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return
            asset.intelligence_status = "indexing"
            await session.commit()
            try:
                user = await session.get(User, asset.user_id) if asset.encryption_version else None
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
                asset = await session.get(Asset, asset_id)
                if asset is not None:
                    asset.intelligence_status = "failed"
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="index_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def index_asset_task(asset_id: str) -> None:
    asyncio.run(index_asset(uuid.UUID(asset_id)))


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

                async def save_batch() -> None:
                    nonlocal batch
                    if not batch:
                        return
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
                        asset = Asset(
                            user_id=library.user_id,
                            original_path=str(path),
                            checksum=checksum,
                            storage_checksum=checksum,
                            encryption_version=0,
                            file_size=size,
                            mime_type=mime_type,
                            processing_status="pending",
                            storage_source="external",
                            external_library_id=library.id,
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
                        await save_batch()
                await save_batch()
                library = await session.get(ExternalLibrary, library_id)
                if library is not None:
                    library.status = "idle"
                    library.file_count = discovered
                    library.last_scanned_at = datetime.now(timezone.utc)
                    await session.commit()
            except Exception as exc:
                await session.rollback()
                library = await session.get(ExternalLibrary, library_id)
                if library is not None:
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
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return
            asset.protection_status = "protecting"
            await session.commit()
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


async def restore_asset(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return
            replica = await session.scalar(
                select(AssetReplica).where(AssetReplica.asset_id == asset.id, AssetReplica.status == "verified")
            )
            if replica is None:
                asset.restore_status = "failed"
                await session.commit()
                return
            asset.restore_status = "restoring"
            await session.commit()
            try:
                source = validated_storage_path(replica.path, settings.replica_path)
                if await asyncio.to_thread(sha256_file, source) != asset.storage_checksum:
                    raise ValueError("Protection copy failed checksum verification")
                destination = original_path_for(
                    asset.checksum,
                    asset.original_filename,
                    asset.user_id if asset.encryption_version else None,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restore")
                with source.open("rb") as input_file, temporary.open("xb") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=4 * 1024 * 1024)
                    output_file.flush()
                    os.fsync(output_file.fileno())
                if await asyncio.to_thread(sha256_file, temporary) != asset.storage_checksum:
                    raise ValueError("Restored original failed checksum verification")
                os.replace(temporary, destination)
                timestamp = asset.file_modified_at or asset.taken_at
                if timestamp:
                    os.utime(destination, (timestamp.timestamp(), timestamp.timestamp()))
                asset.original_path = str(destination)
                asset.storage_source = "managed"
                asset.external_library_id = None
                asset.restore_status = "restored"
                asset.restored_at = datetime.now(timezone.utc)
                await session.commit()
            except Exception:
                await session.rollback()
                asset = await session.get(Asset, asset_id)
                if asset is not None:
                    asset.restore_status = "failed"
                    await session.commit()
                raise
    finally:
        await engine.dispose()


@celery_app.task(name="restore_asset", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def restore_asset_task(asset_id: str) -> None:
    asyncio.run(restore_asset(uuid.UUID(asset_id)))


async def monitor_storage() -> None:
    """Verify originals and protection copies and queue safe, automatic recovery."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            assets = (await session.scalars(select(Asset))).all()
            for asset in assets:
                original = Path(asset.original_path)
                replica = await session.scalar(select(AssetReplica).where(AssetReplica.asset_id == asset.id))
                original_ok = original.is_file() and await asyncio.to_thread(sha256_file, original) == asset.storage_checksum
                replica_ok = bool(replica and Path(replica.path).is_file() and await asyncio.to_thread(sha256_file, Path(replica.path)) == asset.storage_checksum)
                if not original_ok and replica_ok and asset.storage_source == "managed":
                    already_open = await session.scalar(select(MonitoringEvent).where(
                        MonitoringEvent.asset_id == asset.id,
                        MonitoringEvent.kind == "automatic_restore",
                        MonitoringEvent.status == "open",
                    ))
                    if already_open is None:
                        session.add(MonitoringEvent(
                            user_id=asset.user_id, asset_id=asset.id, kind="automatic_restore",
                            severity="warning", message="Original was missing; restore queued from verified protection copy.",
                        ))
                        await notify_user_devices(
                            session,
                            asset.user_id,
                            title="Storage recovery started",
                            body="Drivebound is restoring a missing original from its verified protection copy.",
                            data={"kind": "storage_warning", "asset_id": str(asset.id)},
                        )
                        restore_asset_task.delay(str(asset.id))
                elif original_ok and not replica_ok and settings.auto_protect_uploads:
                    asset.protection_status = "pending"
                    protect_asset_task.delay(str(asset.id))
                elif original_ok and replica_ok:
                    events = (await session.scalars(select(MonitoringEvent).where(
                        MonitoringEvent.asset_id == asset.id, MonitoringEvent.status == "open"
                    ))).all()
                    for event in events:
                        event.status = "resolved"
                        event.resolved_at = datetime.now(timezone.utc)
            await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name="monitor_storage")
def monitor_storage_task() -> None:
    asyncio.run(monitor_storage())


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
                .where(Asset.encryption_version == 0, Asset.storage_source == "managed")
                .order_by(Asset.created_at)
                .limit(limit)
            )).all())
            for asset in assets:
                source = validated_storage_path(asset.original_path, settings.originals_path)
                if not source.is_file():
                    continue
                user = await session.get(User, asset.user_id)
                if user is None:
                    continue
                destination = original_path_for(asset.checksum, asset.original_filename, asset.user_id)
                try:
                    if not destination.exists():
                        await asyncio.to_thread(encrypt_file, source, destination, user_media_key(user))
                    encrypted_thumbnail: Path | None = None
                    old_thumbnail = Path(asset.thumbnail_path) if asset.thumbnail_path else None
                    if old_thumbnail and old_thumbnail.is_file():
                        encrypted_thumbnail = derivative_path_for("thumbnails", asset.checksum, asset.user_id)
                        if not encrypted_thumbnail.exists():
                            await asyncio.to_thread(encrypt_file, old_thumbnail, encrypted_thumbnail, user_media_key(user))
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
                    asset.original_path = str(destination)
                    asset.storage_checksum = await asyncio.to_thread(sha256_file, destination)
                    asset.encryption_version = 1
                    if encrypted_thumbnail:
                        asset.thumbnail_path = str(encrypted_thumbnail)
                    asset.protection_status = "unprotected"
                    await session.commit()
                    if remaining_originals is None:
                        source.unlink(missing_ok=True)
                    if old_thumbnail and remaining_thumbnails is None:
                        old_thumbnail.unlink(missing_ok=True)
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
