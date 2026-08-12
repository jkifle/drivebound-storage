import uuid
from pydantic import BaseModel
from app.schemas.asset import TimelineAssetResponse


class SemanticResult(BaseModel):
    score: float
    excerpt: str
    asset: TimelineAssetResponse


class ReindexResponse(BaseModel):
    queued: int
