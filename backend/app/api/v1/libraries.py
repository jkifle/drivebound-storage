import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import current_user
from app.models.external_library import ExternalLibrary
from app.models.user import User
from app.schemas.library import LibraryCreate, LibraryResponse
from app.services.storage import validated_external_path
from app.worker.tasks import scan_library_task

router = APIRouter(prefix="/libraries", tags=["libraries"])


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
    path = validated_external_path(payload.path)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="External library path is not a readable directory")
    existing = await session.scalar(
        select(ExternalLibrary).where(ExternalLibrary.user_id == user.id, ExternalLibrary.path == str(path))
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="That path is already indexed")
    library = ExternalLibrary(user_id=user.id, name=payload.name.strip(), path=str(path))
    session.add(library)
    await session.commit()
    await session.refresh(library)
    return library


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
    library.status = "queued"
    library.error = None
    await session.commit()
    try:
        scan_library_task.delay(str(library.id))
    except Exception as exc:
        library.status = "failed"
        library.error = f"Could not enqueue scan: {exc}"[:2000]
        await session.commit()
    return library
