import asyncio

from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine

router = APIRouter(tags=["health"])


async def check_postgres() -> bool:
    try:
        async with asyncio.timeout(3):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def check_redis() -> bool:
    client = Redis.from_url(settings.redis_url)
    try:
        async with asyncio.timeout(3):
            return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


@router.get("/health")
async def health(response: Response) -> dict[str, object]:
    postgres_ok, redis_ok = await asyncio.gather(check_postgres(), check_redis())
    healthy = postgres_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "unhealthy",
        "deployment_mode": settings.deployment_mode,
        "media_encryption": "enabled" if settings.media_encryption_enabled else "disabled",
        "services": {
            "postgres": "ok" if postgres_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    }
