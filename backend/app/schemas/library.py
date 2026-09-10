import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    path: str = Field(min_length=1)


class LibraryConnect(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class LibraryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    path: str
    status: str
    file_count: int
    last_scanned_at: datetime | None
    error: str | None
    created_at: datetime


class LibrarySetupResponse(BaseModel):
    configured: bool
    can_connect: bool
    folder_name: str | None = None
    library: LibraryResponse | None = None
    storage_available: bool
    message: str
