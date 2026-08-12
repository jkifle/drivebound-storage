import uuid
from datetime import datetime
from typing import Literal

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


class DevicePushTokenUpdate(BaseModel):
    token: str | None = Field(default=None, max_length=255)


class BackupEventNotification(BaseModel):
    kind: Literal["completed", "failed"]
    uploaded: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=240)
