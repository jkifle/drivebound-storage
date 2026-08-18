"""Private, deterministic media relationship and near-duplicate grouping."""

import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.models.media_group import MediaGroup, MediaGroupMember

RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".orf", ".raf", ".rw2", ".pef"}
STILL_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}


def perceptual_hash(image: Image.Image) -> str:
    """dHash: compact and stable enough for candidate grouping, not identity."""
    pixels = ImageOps.grayscale(image).resize((9, 8), Image.Resampling.LANCZOS)
    values = list(pixels.getdata())
    bits = "".join(
        "1" if values[row * 9 + column] > values[row * 9 + column + 1] else "0"
        for row in range(8) for column in range(8)
    )
    return f"{int(bits, 2):016x}"


def hamming_distance(first: str, second: str) -> int:
    if len(first) != len(second):
        return max(len(first), len(second)) * 4
    return (int(first, 16) ^ int(second, 16)).bit_count()


def normalized_stem(filename: str | None) -> str:
    stem = Path(filename or "").stem.lower()
    stem = re.sub(r"(?:[_ -](?:edited|copy|small|large)|\(\d+\))+$", "", stem)
    return re.sub(r"[^a-z0-9]+", "", stem)


async def _group(session: AsyncSession, asset: Asset, kind: str, key: str, members: list[tuple[Asset, str, float | None]], confidence: float) -> None:
    group = await session.scalar(select(MediaGroup).where(
        MediaGroup.user_id == asset.user_id, MediaGroup.kind == kind, MediaGroup.group_key == key
    ))
    if group is None:
        group = MediaGroup(user_id=asset.user_id, kind=kind, group_key=key[:255], confidence=confidence)
        session.add(group)
        await session.flush()
    for candidate, role, similarity in members:
        present = await session.scalar(select(MediaGroupMember.id).where(
            MediaGroupMember.group_id == group.id, MediaGroupMember.asset_id == candidate.id
        ))
        if present is None:
            session.add(MediaGroupMember(group_id=group.id, asset_id=candidate.id, role=role, similarity=similarity))


async def group_asset(session: AsyncSession, asset: Asset) -> None:
    """Build groups using only account-local metadata and image content."""
    if asset.lifecycle_state == "trashed":
        return
    extension = Path(asset.original_filename or "").suffix.lower()
    stem = normalized_stem(asset.original_filename)
    if not stem:
        return
    candidates = list((await session.scalars(
        select(Asset).where(Asset.user_id == asset.user_id, Asset.id != asset.id, Asset.lifecycle_state != "trashed")
    )).all())

    raw_pair = [candidate for candidate in candidates if normalized_stem(candidate.original_filename) == stem and (
        (extension in RAW_EXTENSIONS and Path(candidate.original_filename or "").suffix.lower() in STILL_EXTENSIONS)
        or (extension in STILL_EXTENSIONS and Path(candidate.original_filename or "").suffix.lower() in RAW_EXTENSIONS)
    )]
    if raw_pair:
        await _group(session, asset, "raw_jpeg", f"raw:{stem}", [(asset, "raw" if extension in RAW_EXTENSIONS else "render", None), *[
            (candidate, "render" if extension in RAW_EXTENSIONS else "raw", None) for candidate in raw_pair
        ]], 1.0)

    live_pair = [candidate for candidate in candidates if normalized_stem(candidate.original_filename) == stem and (
        (extension in VIDEO_EXTENSIONS and Path(candidate.original_filename or "").suffix.lower() in STILL_EXTENSIONS)
        or (extension in STILL_EXTENSIONS and Path(candidate.original_filename or "").suffix.lower() in VIDEO_EXTENSIONS)
    )]
    if live_pair:
        await _group(session, asset, "live_photo", f"live:{stem}", [(asset, "motion" if extension in VIDEO_EXTENSIONS else "still", None), *[
            (candidate, "still" if extension in VIDEO_EXTENSIONS else "motion", None) for candidate in live_pair
        ]], 1.0)

    if "burst" in stem or "img" in stem:
        near_time = [candidate for candidate in candidates if candidate.taken_at and asset.taken_at and candidate.mime_type.startswith("image/") and asset.mime_type.startswith("image/") and abs((candidate.taken_at - asset.taken_at).total_seconds()) <= 3]
        if near_time:
            key = f"burst:{asset.taken_at.strftime('%Y%m%d%H%M%S')}:{asset.camera_model or ''}"
            await _group(session, asset, "burst", key, [(asset, "member", None), *[(candidate, "member", None) for candidate in near_time]], 0.85)

    if asset.perceptual_hash:
        for candidate in candidates:
            if not candidate.perceptual_hash:
                continue
            distance = hamming_distance(asset.perceptual_hash, candidate.perceptual_hash)
            if distance <= 8:
                ordered = sorted((str(asset.id), str(candidate.id)))
                await _group(
                    session, asset, "near_duplicate", f"similar:{ordered[0]}:{ordered[1]}",
                    [(asset, "candidate", 1 - distance / 64), (candidate, "candidate", 1 - distance / 64)],
                    1 - distance / 64,
                )
