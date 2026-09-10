"""Short-lived backend readiness signals; no names, paths, or secrets exposed."""

import hashlib
import math
import time
from typing import Literal

from redis import Redis as SyncRedis
from redis.asyncio import Redis

from app.core.config import settings
from app.services.host_storage import load_storage_manifest

HeartbeatRole = Literal["worker", "scheduler"]
HEARTBEAT_MAX_AGE = 75
BACKUP_LOCK_SECONDS = 7200


def installation_key() -> str:
    manifest = load_storage_manifest()
    identity = manifest.installation_id if manifest is not None else "manual"
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def heartbeat_key(role: HeartbeatRole) -> str:
    return f"drivebound:ready:{installation_key()}:{role}"


def backup_lock_key() -> str:
    return f"drivebound:backup-run:{installation_key()}"


def publish_heartbeat(role: HeartbeatRole) -> None:
    # Heartbeat failure makes /ready fail rather than terminating a worker that
    # still owns an in-flight operation. Each attempt is tightly bounded.
    try:
        with SyncRedis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1) as client:
            client.set(heartbeat_key(role), str(time.time()), ex=HEARTBEAT_MAX_AGE)
    except Exception:
        pass


def heartbeat_is_fresh(value: bytes | str | None, *, now: float | None = None) -> bool:
    try:
        timestamp = float(value)  # type: ignore[arg-type]
        age = (time.time() if now is None else now) - timestamp
        return math.isfinite(timestamp) and 0 <= age <= HEARTBEAT_MAX_AGE
    except (TypeError, ValueError, OverflowError):
        return False


async def background_readiness() -> dict[str, str]:
    client = Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
    try:
        values = await client.mget(heartbeat_key("worker"), heartbeat_key("scheduler"))
        return {role: "ok" if heartbeat_is_fresh(value) else "unavailable"
                for role, value in zip(("worker", "scheduler"), values, strict=True)}
    except Exception:
        return {"worker": "unavailable", "scheduler": "unavailable"}
    finally:
        await client.aclose()
