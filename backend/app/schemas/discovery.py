import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FileItem(BaseModel):
    id: uuid.UUID
    name: str
    relative_path: str | None
    mime_type: str
    file_size: int
    created_at: datetime
    taken_at: datetime | None
    file_modified_at: datetime | None
    latitude: float | None
    longitude: float | None
    protection_status: str
    original_url: str
    thumbnail_url: str | None


class AlbumCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class AlbumUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class AlbumResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    asset_count: int = 0
    cover_thumbnail_url: str | None = None
    role: str = "owner"


class MapItem(BaseModel):
    id: uuid.UUID
    latitude: float
    longitude: float
    taken_at: datetime | None
    thumbnail_url: str | None
    name: str


class AlbumInviteCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default="viewer", pattern="^(viewer|editor)$")


class AlbumInviteResponse(BaseModel):
    message: str
    development_url: str | None = None


class AlbumMemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    joined_at: datetime | None = None
