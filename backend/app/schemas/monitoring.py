import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MonitoringEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    asset_id: uuid.UUID | None
    kind: str
    severity: str
    status: str
    message: str
    detail: dict | None
    resolved_at: datetime | None
    created_at: datetime


class MonitoringOverview(BaseModel):
    open_events: int
    failed_assets: int
    unprotected_assets: int
    events: list[MonitoringEventResponse]


class RecoveryVerificationQueued(BaseModel):
    status: Literal["queued"] = "queued"
    scope: Literal["account"] = "account"
    mode: Literal["verify_only", "verify_and_repair"]
    verification_id: uuid.UUID
    job_id: str
