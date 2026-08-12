import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ShareCreate(BaseModel):
    asset_id: uuid.UUID
    password: str | None = Field(default=None, min_length=8, max_length=128)
    allow_download: bool = True
    expires_in_hours: int | None = Field(default=168, ge=1, le=24 * 365)


class ShareResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    asset_id: uuid.UUID
    allow_download: bool
    expires_at: datetime | None
    created_at: datetime
    password_required: bool = False
    url: str | None = None


class PublicShareResponse(BaseModel):
    asset_id: uuid.UUID
    filename: str | None
    mime_type: str
    created_at: datetime
    expires_at: datetime | None
    password_required: bool
    allow_download: bool
    content_url: str
