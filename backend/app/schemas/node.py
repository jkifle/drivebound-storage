import uuid
import json
import math
from datetime import datetime
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


MAX_CAPABILITY_KEYS = 64
MAX_CAPABILITY_BYTES = 16 * 1024
MAX_CAPABILITY_DEPTH = 8
MAX_CAPABILITY_MEMBERS = 256


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class NodeClaim(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    name: str = Field(min_length=1, max_length=255)
    public_key: str = Field(min_length=43, max_length=43)
    timestamp_ms: int = Field(gt=0)
    signature: str = Field(min_length=86, max_length=86)
    endpoint_url: AnyHttpUrl | None = None


class NodeRegistration(BaseModel):
    node_id: uuid.UUID
    node_secret: str
    attestation_state: str


class NodeHeartbeat(BaseModel):
    node_id: uuid.UUID
    timestamp_ms: int = Field(gt=0)
    version: str | None = Field(default=None, max_length=64)
    capabilities: dict[str, object] = Field(default_factory=dict)
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("capabilities")
    @classmethod
    def bounded_capabilities(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > MAX_CAPABILITY_KEYS:
            raise ValueError(f"capabilities may contain at most {MAX_CAPABILITY_KEYS} top-level keys")
        members = 0
        pending: list[tuple[object, int]] = [(value, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > MAX_CAPABILITY_DEPTH:
                raise ValueError(f"capabilities may be nested at most {MAX_CAPABILITY_DEPTH} levels")
            if isinstance(current, dict):
                members += len(current)
                for key, child in current.items():
                    if not isinstance(key, str) or not key or len(key) > 128:
                        raise ValueError("capability keys must contain 1 to 128 characters")
                    pending.append((child, depth + 1))
            elif isinstance(current, list):
                members += len(current)
                pending.extend((child, depth + 1) for child in current)
            elif current is None or isinstance(current, (str, bool, int)):
                pass
            elif isinstance(current, float) and math.isfinite(current):
                pass
            else:
                raise ValueError("capabilities must contain only JSON objects, arrays, and scalar values")
            if members > MAX_CAPABILITY_MEMBERS:
                raise ValueError(f"capabilities may contain at most {MAX_CAPABILITY_MEMBERS} members")
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("capabilities must contain finite JSON values") from exc
        if len(encoded) > MAX_CAPABILITY_BYTES:
            raise ValueError(f"canonical capabilities may be at most {MAX_CAPABILITY_BYTES} bytes")
        return value


class NodeSecretRotation(BaseModel):
    node_id: uuid.UUID
    timestamp_ms: int = Field(gt=0)
    signature: str = Field(min_length=86, max_length=86)


class NodeSecretRotationResponse(BaseModel):
    node_id: uuid.UUID
    node_secret: str
    rotated_at: datetime


class NodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    endpoint_url: str | None
    status: str
    version: str | None
    capabilities: dict[str, object] | None
    attestation_state: str
    last_attested_at: datetime | None
    secret_rotated_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
