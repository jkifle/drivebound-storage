import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.assets import timeline_item
from app.core.security import current_user_or_device
from app.db.session import get_db
from app.models.asset import Asset
from app.models.user import User
from app.schemas.intelligence import ReindexResponse, SemanticResult
from app.services.intelligence import embed_text
from app.worker.tasks import index_asset_task

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


@router.get("/search", response_model=list[SemanticResult])
async def semantic_search(
    query: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> list[SemanticResult]:
    query_vector = await asyncio.to_thread(embed_text, query, True)
    distance = Asset.embedding.cosine_distance(query_vector).label("distance")
    ranked = (await session.execute(select(Asset, distance).where(
        Asset.user_id == user.id, Asset.embedding.is_not(None)
    ).order_by(distance).limit(limit))).all()
    return [SemanticResult(score=max(0.0, 1.0 - float(score)), excerpt=(asset.ocr_text or asset.semantic_text or "")[:240], asset=timeline_item(asset)) for asset, score in ranked]


@router.post("/reindex", response_model=ReindexResponse)
async def reindex(
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user_or_device),
) -> ReindexResponse:
    asset_ids = (await session.scalars(select(Asset.id).where(Asset.user_id == user.id))).all()
    for asset_id in asset_ids:
        index_asset_task.delay(str(asset_id))
    return ReindexResponse(queued=len(asset_ids))
