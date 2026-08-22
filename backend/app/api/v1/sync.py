import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.db.session import get_db
from app.models.sync import SyncConflict, SyncOperation, SyncRoot
from app.models.user import User
from app.schemas.sync import (
    SyncConflictResolution,
    SyncConflictResponse,
    SyncOperationBatch,
    SyncOperationResponse,
    SyncPushResponse,
    SyncRootCreate,
    SyncRootResponse,
)
from app.services.sync import apply_operations, resolve_conflict

router = APIRouter(prefix="/sync", tags=["sync"])


async def owned_root(session: AsyncSession, user: User, root_id: uuid.UUID) -> SyncRoot:
    root = await session.scalar(select(SyncRoot).where(SyncRoot.id == root_id, SyncRoot.user_id == user.id))
    if root is None:
        raise HTTPException(status_code=404, detail="Sync root not found")
    return root


def operation_response(operation: SyncOperation) -> SyncOperationResponse:
    return SyncOperationResponse(
        id=operation.id, cursor=operation.cursor, client_id=operation.client_id, operation_id=operation.operation_id, kind=operation.kind,
        logical_id=operation.logical_id, base_revision=operation.base_revision, revision=operation.revision,
        payload=operation.payload, status=operation.status, created_at=operation.created_at,
    )


@router.get("/roots", response_model=list[SyncRootResponse])
async def list_roots(session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> list[SyncRoot]:
    return list((await session.scalars(select(SyncRoot).where(SyncRoot.user_id == user.id).order_by(SyncRoot.created_at))).all())


@router.post("/roots", response_model=SyncRootResponse, status_code=status.HTTP_201_CREATED)
async def register_root(
    payload: SyncRootCreate, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> SyncRoot:
    root = await session.scalar(select(SyncRoot).where(SyncRoot.user_id == user.id, SyncRoot.client_id == payload.client_id))
    if root is None:
        root = SyncRoot(user_id=user.id, name=payload.name.strip(), client_id=payload.client_id)
        session.add(root)
    else:
        root.name = payload.name.strip()
    await session.commit()
    await session.refresh(root)
    return root


@router.get("/roots/{root_id}/operations", response_model=SyncPushResponse)
async def pull_operations(
    root_id: uuid.UUID, cursor: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=1000),
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> SyncPushResponse:
    root = await owned_root(session, user, root_id)
    operations = list((await session.scalars(
        select(SyncOperation).where(SyncOperation.root_id == root.id, SyncOperation.cursor > cursor).order_by(SyncOperation.cursor).limit(limit)
    )).all())
    conflicts = list((await session.scalars(
        select(SyncConflict).where(SyncConflict.root_id == root.id, SyncConflict.status == "open").order_by(SyncConflict.created_at)
    )).all())
    return SyncPushResponse(cursor=root.cursor, operations=[operation_response(operation) for operation in operations], conflicts=[conflict.id for conflict in conflicts])


@router.post("/roots/{root_id}/operations", response_model=SyncPushResponse)
async def push_operations(
    root_id: uuid.UUID, payload: SyncOperationBatch,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> SyncPushResponse:
    root = await owned_root(session, user, root_id)
    try:
        operations, conflicts = await apply_operations(session, root, payload.client_id, payload.operations)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return SyncPushResponse(cursor=root.cursor, operations=[operation_response(operation) for operation in operations], conflicts=[conflict.id for conflict in conflicts])


@router.get("/roots/{root_id}/conflicts", response_model=list[SyncConflictResponse])
async def list_conflicts(
    root_id: uuid.UUID, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> list[SyncConflict]:
    root = await owned_root(session, user, root_id)
    return list((await session.scalars(
        select(SyncConflict).where(SyncConflict.root_id == root.id, SyncConflict.status == "open").order_by(SyncConflict.created_at)
    )).all())


@router.post("/conflicts/{conflict_id}/resolve", response_model=SyncConflictResponse)
async def resolve_sync_conflict(
    conflict_id: uuid.UUID, payload: SyncConflictResolution,
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> SyncConflict:
    conflict = await session.scalar(
        select(SyncConflict).join(SyncRoot, SyncRoot.id == SyncConflict.root_id).where(SyncConflict.id == conflict_id, SyncRoot.user_id == user.id)
    )
    if conflict is None:
        raise HTTPException(status_code=404, detail="Sync conflict not found")
    try:
        await resolve_conflict(session, conflict, payload.choice)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return conflict
