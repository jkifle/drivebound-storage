from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.db.session import get_db
from app.models.asset import Asset
from app.models.monitoring import MonitoringEvent
from app.models.user import User
from app.schemas.monitoring import MonitoringOverview
from app.worker.tasks import monitor_storage_task

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.get("", response_model=MonitoringOverview)
async def overview(session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> MonitoringOverview:
    events = (await session.scalars(select(MonitoringEvent).where(MonitoringEvent.user_id == user.id).order_by(MonitoringEvent.created_at.desc()).limit(50))).all()
    open_events = await session.scalar(select(func.count()).select_from(MonitoringEvent).where(MonitoringEvent.user_id == user.id, MonitoringEvent.status == "open"))
    failed = await session.scalar(select(func.count()).select_from(Asset).where(Asset.user_id == user.id, Asset.processing_status == "failed"))
    unprotected = await session.scalar(select(func.count()).select_from(Asset).where(Asset.user_id == user.id, Asset.protection_status != "protected"))
    return MonitoringOverview(open_events=open_events or 0, failed_assets=failed or 0, unprotected_assets=unprotected or 0, events=list(events))


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_monitor(user: User = Depends(current_user)) -> dict[str, str]:
    monitor_storage_task.delay()
    return {"status": "queued"}
