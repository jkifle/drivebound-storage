import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import current_user
from app.core.observability import metrics
from app.db.session import get_db
from app.models.asset import Asset
from app.models.monitoring import MonitoringEvent
from app.models.user import User
from app.schemas.monitoring import MonitoringOverview, RecoveryVerificationQueued
from app.worker.tasks import monitor_storage_task

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
VERIFICATION_LOCK_MINUTES = 60


@router.get("", response_model=MonitoringOverview)
async def overview(session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> MonitoringOverview:
    events = (await session.scalars(select(MonitoringEvent).where(MonitoringEvent.user_id == user.id).order_by(MonitoringEvent.created_at.desc()).limit(50))).all()
    open_events = await session.scalar(select(func.count()).select_from(MonitoringEvent).where(MonitoringEvent.user_id == user.id, MonitoringEvent.status == "open"))
    failed = await session.scalar(select(func.count()).select_from(Asset).where(Asset.user_id == user.id, Asset.processing_status == "failed"))
    unprotected = await session.scalar(select(func.count()).select_from(Asset).where(Asset.user_id == user.id, Asset.protection_status != "protected"))
    return MonitoringOverview(open_events=open_events or 0, failed_assets=failed or 0, unprotected_assets=unprotected or 0, events=list(events))


def verification_lock_active(event: MonitoringEvent, now: datetime | None = None) -> bool:
    if event.status != "open":
        return False
    # Once Celery acknowledges a job, only that exact worker completion may
    # release the lock. The cooldown exists solely for a process that dies in
    # the narrow window between creating the DB lock and recording a job ID.
    if event.detail and event.detail.get("job_id"):
        return True
    if event.created_at is None:
        return True
    reference = now or datetime.now(timezone.utc)
    created_at = event.created_at if event.created_at.tzinfo else event.created_at.replace(tzinfo=timezone.utc)
    return created_at > reference - timedelta(minutes=VERIFICATION_LOCK_MINUTES)


async def queue_account_verification(
    session: AsyncSession,
    user: User,
    *,
    repair: bool,
) -> RecoveryVerificationQueued:
    existing = await session.scalar(
        select(MonitoringEvent)
        .where(
            MonitoringEvent.user_id == user.id,
            MonitoringEvent.kind == "account_storage_verification",
            MonitoringEvent.status == "open",
        )
        .with_for_update()
    )
    if existing is not None and verification_lock_active(existing):
        raise HTTPException(status_code=409, detail="A storage verification is already running for this account")
    if existing is not None:
        existing.status = "resolved"
        existing.resolved_at = datetime.now(timezone.utc)
        existing.detail = {**(existing.detail or {}), "outcome": "stale_lock_released"}
        await session.flush()

    verification = MonitoringEvent(
        id=uuid.uuid4(),
        user_id=user.id,
        asset_id=None,
        kind="account_storage_verification",
        severity="info",
        message="Account storage verification is running.",
        detail={"mode": "verify_and_repair" if repair else "verify_only"},
    )
    session.add(verification)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A storage verification is already running for this account") from exc

    try:
        job = monitor_storage_task.delay(str(user.id), repair, str(verification.id))
    except Exception as exc:
        metrics.operations.observe_recovery("storage_verification", "failed")
        verification.status = "failed"
        verification.resolved_at = datetime.now(timezone.utc)
        verification.detail = {**(verification.detail or {}), "outcome": "queue_failed"}
        await session.commit()
        raise HTTPException(status_code=503, detail="Storage verification could not be queued") from exc
    verification.detail = {**(verification.detail or {}), "job_id": str(job.id)}
    await session.commit()
    metrics.operations.observe_recovery("storage_verification", "queued")
    return RecoveryVerificationQueued(
        mode="verify_and_repair" if repair else "verify_only",
        verification_id=verification.id,
        job_id=str(job.id),
    )


@router.post("/verify", response_model=RecoveryVerificationQueued, status_code=status.HTTP_202_ACCEPTED)
async def verify_account_storage(
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> RecoveryVerificationQueued:
    """Checksum account media and replicas without writing or deleting media bytes."""
    return await queue_account_verification(session, user, repair=False)


@router.post("/run", response_model=RecoveryVerificationQueued, status_code=status.HTTP_202_ACCEPTED)
async def run_monitor(
    session: AsyncSession = Depends(get_db), user: User = Depends(current_user)
) -> RecoveryVerificationQueued:
    """Verify this account and queue safe copy repair/restoration where possible."""
    return await queue_account_verification(session, user, repair=True)
