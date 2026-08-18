import uuid
from datetime import datetime

from pydantic import BaseModel


class MediaGroupMemberResponse(BaseModel):
    asset_id: uuid.UUID
    role: str
    similarity: float | None


class MediaGroupResponse(BaseModel):
    id: uuid.UUID
    kind: str
    confidence: float
    created_at: datetime
    members: list[MediaGroupMemberResponse]
