import uuid
from datetime import datetime
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class NodeClaim(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    name: str = Field(min_length=1, max_length=255)
    public_key: str = Field(min_length=16, max_length=10000)
    endpoint_url: AnyHttpUrl | None = None


class NodeRegistration(BaseModel):
    node_id: uuid.UUID
    node_secret: str


class NodeHeartbeat(BaseModel):
    version: str | None = Field(default=None, max_length=64)
    capabilities: dict[str, object] = {}


class NodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    endpoint_url: str | None
    status: str
    version: str | None
    capabilities: dict[str, object] | None
    last_seen_at: datetime | None
    created_at: datetime
