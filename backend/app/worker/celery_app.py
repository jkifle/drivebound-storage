import time

from celery import Celery, signals

from app.core.config import settings
from app.core.observability import bounded_task_name, log_operation, metrics

celery_app = Celery("photos", broker=settings.redis_url, backend=settings.redis_url)


class ObservableTask(celery_app.Task):
    """Record broker publish failures at one boundary for every task."""

    abstract = True

    def apply_async(self, *args: object, **kwargs: object):
        try:
            return super().apply_async(*args, **kwargs)
        except Exception:
            metrics.operations.observe_queue_publish(self.name, "failed")
            raise


celery_app.Task = ObservableTask
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    beat_schedule={
        "verify-storage-and-recover": {
            "task": "monitor_storage",
            "schedule": settings.monitor_interval_seconds,
        },
        "purge-expired-trash": {
            "task": "purge_expired_assets",
            "schedule": settings.lifecycle_purge_interval_seconds,
        },
        "backup-and-verify-operational-state": {
            "task": "backup_operational_state",
            "schedule": settings.database_backup_interval_hours * 60 * 60,
        }
    } | ({
        "migrate-legacy-media-encryption": {
            "task": "migrate_legacy_media",
            "schedule": 60,
        }
    } if settings.media_encryption_migrate_legacy else {}),
)
celery_app.autodiscover_tasks(["app.worker"])


@signals.before_task_publish.connect
def stamp_task_publish(headers: dict | None = None, **_kwargs: object) -> None:
    """Carry one non-sensitive timestamp for broker-wait telemetry."""
    if headers is not None:
        headers["drivebound_published_at"] = time.time()


@signals.after_task_publish.connect
def record_task_publish(sender: object = None, **_kwargs: object) -> None:
    metrics.operations.observe_queue_publish(sender, "published")


@signals.task_prerun.connect
def record_task_start(sender: object = None, task: object = None, **_kwargs: object) -> None:
    task_object = task or sender
    if task_object is None:
        return
    request = getattr(task_object, "request", None)
    if request is None:
        return
    # Celery Task instances are reusable and can execute concurrently in
    # thread/gevent pools. The request context is execution-local, so timing
    # state must live there rather than on the Task singleton.
    setattr(request, "_drivebound_started_at", time.monotonic())
    headers = getattr(request, "headers", None) or {}
    published_at = headers.get("drivebound_published_at")
    if isinstance(published_at, (int, float)):
        metrics.operations.observe_queue_wait(sender, max(time.time() - float(published_at), 0.0))


@signals.task_postrun.connect
def record_task_completion(sender: object = None, task: object = None, state: object = None, **_kwargs: object) -> None:
    task_object = task or sender
    request = getattr(task_object, "request", None)
    started_at = getattr(request, "_drivebound_started_at", None)
    if request is not None and hasattr(request, "_drivebound_started_at"):
        delattr(request, "_drivebound_started_at")
    duration = max(time.monotonic() - started_at, 0.0) if isinstance(started_at, (int, float)) else 0.0
    outcome = {
        "SUCCESS": "success",
        "RETRY": "retry",
        "REVOKED": "revoked",
    }.get(str(state).upper(), "failure")
    task_name = bounded_task_name(getattr(sender, "name", sender))
    metrics.operations.observe_worker(task_name, outcome, duration)
    log_operation(component="worker", operation=task_name, outcome=outcome, duration_seconds=duration)


@signals.heartbeat_sent.connect
def record_worker_heartbeat(**_kwargs: object) -> None:
    metrics.operations.worker_heartbeat()
