"""Report real Celery beat ticks independently from its dispatched jobs."""

import time

from celery.beat import PersistentScheduler

from app.services.readiness import publish_heartbeat


class PilotScheduler(PersistentScheduler):
    _last_readiness_heartbeat: float = 0

    def tick(self, *args, **kwargs):
        delay = super().tick(*args, **kwargs)
        now = time.monotonic()
        if now - self._last_readiness_heartbeat >= 15:
            publish_heartbeat("scheduler")
            self._last_readiness_heartbeat = now
        return min(delay, 15)
