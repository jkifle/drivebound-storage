import hashlib
import os
import shutil
import uuid
from pathlib import Path

import aiofiles
from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

def original_path_for(checksum: str, filename: str | None) -> Path:
    # Content identity, not a client-controlled filename, determines the path.
    # Keeping the path extensionless makes concurrent identical uploads contend
    # for one exclusive-create operation even when their filenames differ.
    return settings.originals_path / checksum[:2] / checksum


async def stage_upload(upload: UploadFile) -> tuple[Path, str, int]:
    settings.staging_path.mkdir(parents=True, exist_ok=True)
    temporary_path = settings.staging_path / f"{uuid.uuid4()}.upload"
    digest = hashlib.sha256()
    size = 0

    try:
        async with aiofiles.open(temporary_path, "xb") as output:
            while chunk := await upload.read(settings.upload_chunk_size):
                size += len(chunk)
                if size > settings.max_upload_size:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Upload exceeds the configured size limit",
                    )
                digest.update(chunk)
                await output.write(chunk)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        temporary_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Empty files are not accepted")
    return temporary_path, digest.hexdigest(), size


def commit_original(staged_path: Path, final_path: Path) -> bool:
    """Create an original exactly once; never overwrite an existing path."""
    final_path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with staged_path.open("rb") as source, final_path.open("xb") as destination:
            created = True
            shutil.copyfileobj(source, destination, length=4 * 1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
    except FileExistsError:
        pass
    except Exception:
        if created:
            # The path was never published as a database-backed original.
            final_path.unlink(missing_ok=True)
        raise
    finally:
        staged_path.unlink(missing_ok=True)
    return created


def validated_storage_path(stored_path: str, root: Path) -> Path:
    path = Path(stored_path).resolve()
    resolved_root = root.resolve()
    if not path.is_relative_to(resolved_root):
        raise HTTPException(status_code=500, detail="Asset has an invalid storage path")
    return path


def validated_external_path(stored_path: str) -> Path:
    path = Path(stored_path).resolve()
    if not any(path.is_relative_to(root) for root in settings.external_root_list):
        raise HTTPException(status_code=400, detail="Path is outside the configured external library roots")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
