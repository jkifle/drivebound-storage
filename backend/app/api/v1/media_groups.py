import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user_or_device
from app.db.session import get_db
from app.models.media_group import MediaGroup, MediaGroupMember
from app.models.user import User
from app.schemas.media_group import MediaGroupMemberResponse, MediaGroupResponse

router = APIRouter(prefix="/media-groups", tags=["media-groups"])


@router.get("", response_model=list[MediaGroupResponse])
async def list_media_groups(
    asset_id: uuid.UUID | None = None,
    kind: str | None = Query(default=None, max_length=32),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> list[MediaGroupResponse]:
    statement = select(MediaGroup).where(MediaGroup.user_id == user.id).order_by(MediaGroup.created_at.desc())
    if kind:
        statement = statement.where(MediaGroup.kind == kind)
    if asset_id:
        statement = statement.join(MediaGroupMember, MediaGroupMember.group_id == MediaGroup.id).where(MediaGroupMember.asset_id == asset_id)
    groups = list((await session.scalars(statement)).all())
    response: list[MediaGroupResponse] = []
    for group in groups:
        members = list((await session.scalars(select(MediaGroupMember).where(MediaGroupMember.group_id == group.id))).all())
        response.append(MediaGroupResponse(
            id=group.id, kind=group.kind, confidence=group.confidence, created_at=group.created_at,
            members=[MediaGroupMemberResponse(asset_id=member.asset_id, role=member.role, similarity=member.similarity) for member in members],
        ))
    return response
