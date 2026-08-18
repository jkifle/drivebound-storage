import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class AssetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    logical_id: uuid.UUID
    version: int
    lifecycle_state: str
    superseded_at: datetime | None
    trashed_at: datetime | None
    purge_after: datetime | None
    checksum: str
    encryption_version: int
    file_size: int
    mime_type: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    taken_at: datetime | None
    file_created_at: datetime | None
    file_modified_at: datetime | None
    latitude: float | None
    longitude: float | None
    camera_make: str | None
    camera_model: str | None
    lens_model: str | None
    orientation: int | None
    metadata_json: dict[str, object] | None
    processing_status: str
    processing_error: str | None
    storage_source: str
    external_library_id: uuid.UUID | None
    original_filename: str | None
    relative_path: str | None
    protection_status: str
    restore_status: str
    restored_at: datetime | None
    perceptual_hash: str | None
    created_at: datetime


class UploadResponse(BaseModel):
    asset: AssetResponse
    duplicate: bool


class TimelineAssetResponse(BaseModel):
    id: uuid.UUID
    mime_type: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    taken_at: datetime | None
    timeline_at: datetime
    day: date
    processing_status: str
    original_url: str
    thumbnail_url: str | None
    original_filename: str | None = None
    protection_status: str = "unprotected"
    restore_status: str = "not_requested"


class TimelineResponse(BaseModel):
    items: list[TimelineAssetResponse]
    next_cursor: str | None
    has_more: bool
