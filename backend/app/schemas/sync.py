import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SyncRootCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    client_id: str = Field(min_length=8, max_length=128)


class SyncRootResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    client_id: str
    cursor: int
    status: str
    last_seen_at: datetime | None
    created_at: datetime


class SyncOperationInput(BaseModel):
    operation_id: uuid.UUID
    client_sequence: int = Field(ge=1)
    kind: Literal["upsert", "move", "delete"]
    logical_id: uuid.UUID
    base_revision: str | None = Field(default=None, max_length=128)
    relative_path: str | None = Field(default=None, max_length=4096)
    asset_id: uuid.UUID | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_only(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/").strip("/")
        if not normalized or normalized.startswith("../") or "/../" in normalized:
            raise ValueError("relative_path must stay within the selected sync root")
        return normalized


class SyncOperationBatch(BaseModel):
    client_id: str = Field(min_length=8, max_length=128)
    operations: list[SyncOperationInput] = Field(min_length=1, max_length=500)


class SyncOperationResponse(BaseModel):
    id: uuid.UUID
    cursor: int
    client_id: str
    operation_id: uuid.UUID
    kind: str
    logical_id: uuid.UUID
    base_revision: str | None
    revision: str | None
    payload: dict[str, object]
    status: str
    created_at: datetime


class SyncPushResponse(BaseModel):
    cursor: int
    operations: list[SyncOperationResponse]
    conflicts: list[uuid.UUID]


class SyncConflictResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    logical_id: uuid.UUID
    kind: str
    detail: dict[str, object] | None
    status: str
    resolution: str | None
    created_at: datetime
    resolved_at: datetime | None


class SyncConflictResolution(BaseModel):
    choice: Literal["keep_local", "keep_remote"]
