import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LifecyclePolicyUpdate(BaseModel):
    desired_replica_count: int | None = Field(default=None, ge=1, le=8)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    balance_threshold_percent: int | None = Field(default=None, ge=1, le=80)
    backup_retention_days: int | None = Field(default=None, ge=1, le=3650)


class LifecyclePolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    desired_replica_count: int
    retention_days: int
    balance_threshold_percent: int
    backup_retention_days: int


class StorageDriveCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1)
    priority: int = Field(default=100, ge=0, le=1000)
    eligible: bool = True


class StorageDriveUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    priority: int | None = Field(default=None, ge=0, le=1000)
    eligible: bool | None = None


class StorageDriveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    path: str
    priority: int
    eligible: bool
    failure_count: int
    verification_status: str
    last_verified_at: datetime | None
    last_error: str | None


class AssetRevisionResponse(BaseModel):
    id: uuid.UUID
    logical_id: uuid.UUID
    version: int
    lifecycle_state: str
    original_filename: str | None
    checksum: str
    file_size: int
    created_at: datetime
    superseded_at: datetime | None
    trashed_at: datetime | None
    purge_after: datetime | None


class TrashResponse(AssetRevisionResponse):
    pass


class BackupArchiveResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    size_bytes: int
    status: str
    verification_status: str
    verified_at: datetime | None
    verification_detail: str | None
    created_at: datetime


class RecoveryReadinessResponse(BaseModel):
    database_backup_enabled: bool
    recoverable: bool
    database_backup_fresh: bool
    database_backup_status: str
    configuration_backup_status: str
    latest_database_backup_at: datetime | None
    latest_configuration_backup_at: datetime | None
    failed_archives: int
    purges_in_progress: int
    retention_days: int
    note: str
