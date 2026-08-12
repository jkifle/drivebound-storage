import asyncio
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.security import current_user
from app.models.user import User

router = APIRouter(prefix="/storage", tags=["storage"])


def inspect_root(name: str, path: Path, role: str) -> dict[str, object]:
    resolved = path.resolve()
    try:
        usage = shutil.disk_usage(resolved)
        writable = os.access(resolved, os.W_OK)
        return {
            "name": name,
            "path": str(resolved),
            "role": role,
            "status": "online",
            "smart_status": "unavailable",
            "writable": writable,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }
    except OSError as exc:
        return {
            "name": name,
            "path": str(resolved),
            "role": role,
            "status": "unavailable",
            "smart_status": "unavailable",
            "writable": False,
            "error": str(exc),
        }


@router.get("")
async def storage_status(_user: User = Depends(current_user)) -> dict[str, object]:
    roots = [
        ("Managed originals", settings.originals_path, "managed"),
        ("Protection copies", settings.replica_path, "replica"),
    ]
    roots.extend((f"External library {index + 1}", root, "external") for index, root in enumerate(settings.external_root_list))
    drives = await asyncio.gather(*(asyncio.to_thread(inspect_root, name, path, role) for name, path, role in roots))
    online = [drive for drive in drives if drive["status"] == "online"]
    return {
        "status": "ok" if online else "unavailable",
        "drives": drives,
        "smart_available": False,
        "health_note": "Filesystem availability is monitored. SMART requires an optional host-level adapter.",
    }
