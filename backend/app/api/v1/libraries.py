import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import current_user
from app.models.external_library import ExternalLibrary
from app.models.user import User
from app.schemas.library import LibraryConnect, LibraryCreate, LibraryResponse, LibrarySetupResponse
from app.services.storage import canonical_storage_path, validated_external_path
from app.services.host_storage import (
    StorageUnavailableError, approved_import_root, configured_owner_email,
    is_host_owner, load_storage_manifest, require_host_owner,
)
from app.worker.tasks import scan_library_task

router = APIRouter(prefix="/libraries", tags=["libraries"])


@router.get("/setup", response_model=LibrarySetupResponse)
async def library_setup(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> LibrarySetupResponse:
    try:
        configured = configured_owner_email() is not None
        if not is_host_owner(user):
            return LibrarySetupResponse(
                configured=configured, can_connect=False, storage_available=False,
                message="The PC owner has not approved this verified account to connect its photo folder.",
            )
        root = approved_import_root(user)
    except StorageUnavailableError:
        return LibrarySetupResponse(configured=True, can_connect=False, storage_available=False,
                                    message="Reconnect the approved photo drive, then retry.")
    except HTTPException as exc:
        return LibrarySetupResponse(configured=True, can_connect=False, storage_available=False, message=str(exc.detail))
    library = await session.scalar(select(ExternalLibrary).where(
        ExternalLibrary.user_id == user.id, ExternalLibrary.path == canonical_storage_path(root),
    ))
    manifest = load_storage_manifest()
    return LibrarySetupResponse(
        configured=True, can_connect=True, folder_name=manifest.import_display_name if manifest else root.name,
        library=LibraryResponse.model_validate(library) if library else None,
        storage_available=True,
        message="Your PC photo folder is ready to connect." if library is None else "Your PC photo folder is connected.",
    )


async def persist_library(session: AsyncSession, user: User, name: str, path: Path, *, reuse: bool = False) -> ExternalLibrary:
    name = name.strip()
    if not name:
        raise HTTPException(422, "Give this photo collection a name.")
    # Serialize same-owner connect requests and fence concurrent account
    # deletion. Host approval is rechecked after the lock before publishing.
    locked_owner = await session.scalar(select(User).where(
        User.id == user.id, User.disabled_at.is_(None),
    ).with_for_update().execution_options(populate_existing=True))
    if locked_owner is None:
        raise HTTPException(401, "Account is unavailable")
    require_host_owner(locked_owner)
    canonical = canonical_storage_path(path)
    libraries = list((await session.scalars(select(ExternalLibrary))).all())
    for existing in libraries:
        existing_path = Path(existing.path).resolve()
        overlaps = path.is_relative_to(existing_path) or existing_path.is_relative_to(path)
        if overlaps and existing.user_id != user.id:
            raise HTTPException(409, "This photo folder is already assigned to another account. Ask the PC owner to review its configuration.")
        if existing.user_id == user.id and existing_path == path:
            if reuse:
                return existing
            raise HTTPException(409, "That photo folder is already connected")
    library = ExternalLibrary(user_id=user.id, name=name, path=canonical)
    session.add(library)
    await session.commit()
    await session.refresh(library)
    return library


@router.post("/connect", response_model=LibraryResponse, status_code=status.HTTP_201_CREATED)
async def connect_library(
    payload: LibraryConnect,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> ExternalLibrary:
    return await persist_library(session, user, payload.name, approved_import_root(user), reuse=True)


@router.get("", response_model=list[LibraryResponse])
async def list_libraries(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> list[ExternalLibrary]:
    return list((await session.scalars(select(ExternalLibrary).where(ExternalLibrary.user_id == user.id).order_by(ExternalLibrary.created_at))).all())


@router.post("", response_model=LibraryResponse, status_code=status.HTTP_201_CREATED)
async def create_library(
    payload: LibraryCreate,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> ExternalLibrary:
    require_host_owner(user)
    path = validated_external_path(payload.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="External library path is not a readable directory")
    return await persist_library(session, user, payload.name, path)


@router.post("/{library_id}/scan", response_model=LibraryResponse, status_code=status.HTTP_202_ACCEPTED)
async def scan_library(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> ExternalLibrary:
    library = await session.scalar(
        select(ExternalLibrary).where(ExternalLibrary.id == library_id, ExternalLibrary.user_id == user.id)
    )
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if configured_owner_email() is not None:
        require_host_owner(user)
    validated_external_path(library.path)
    library.status = "queued"
    library.error = None
    await session.commit()
    try:
        scan_library_task.delay(str(library.id))
    except Exception:
        library.status = "failed"
        library.error = "The photo scan could not start. Check that the background service is running and retry."
        await session.commit()
        raise HTTPException(503, library.error) from None
    return library
