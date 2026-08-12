import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException


@dataclass(frozen=True)
class TimelineCursor:
    timeline_at: datetime
    asset_id: uuid.UUID


def encode_cursor(cursor: TimelineCursor) -> str:
    timestamp = cursor.timeline_at
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    payload = {
        "v": 1,
        "t": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "id": str(cursor.asset_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> TimelineCursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if payload.get("v") != 1:
            raise ValueError("Unsupported cursor version")
        timestamp = datetime.fromisoformat(payload["t"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Cursor timestamp must include a timezone")
        return TimelineCursor(
            timeline_at=timestamp.astimezone(timezone.utc),
            asset_id=uuid.UUID(payload["id"]),
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        raise HTTPException(status_code=400, detail="Invalid timeline cursor") from None

