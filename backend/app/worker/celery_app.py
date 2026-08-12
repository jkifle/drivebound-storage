from celery import Celery

from app.core.config import settings

celery_app = Celery("photos", broker=settings.redis_url, backend=settings.redis_url)
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
        }
    } | ({
        "migrate-legacy-media-encryption": {
            "task": "migrate_legacy_media",
            "schedule": 60,
        }
    } if settings.media_encryption_migrate_legacy else {}),
)
celery_app.autodiscover_tasks(["app.worker"])
