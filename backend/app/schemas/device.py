import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=64)


class DeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    platform: str
    last_seen_at: datetime | None
    last_backup_at: datetime | None
    files_backed_up: int
    bytes_backed_up: int
    created_at: datetime


class DeviceRegistration(DeviceResponse):
    token: str


class BackupStatus(BaseModel):
    exists: bool
    asset_id: uuid.UUID | None = None
