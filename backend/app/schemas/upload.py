import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.asset import AssetResponse


class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(default="application/octet-stream", max_length=255)
    total_size: int = Field(gt=0)
    checksum: str | None = None
    file_created_at: datetime | None = None
    file_modified_at: datetime | None = None

    @field_validator("checksum")
    @classmethod
    def valid_checksum(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value)):
            raise ValueError("checksum must be a SHA-256 hexadecimal value")
        return value.lower() if value else None


class UploadSessionResponse(BaseModel):
    id: uuid.UUID
    upload_url: str
    offset: int
    total_size: int
    chunk_size: int
    status: str
    asset: AssetResponse | None = None
    duplicate: bool = False
