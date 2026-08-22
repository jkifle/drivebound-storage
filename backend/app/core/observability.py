"""Low-cost, bounded observability for API and worker processes.

HTTP metrics are process-local. Operational metrics are also mirrored to the
configured Redis instance so Celery workers and the API metrics endpoint share
one view without adding a database table. Redis failures never affect product
work: the registry keeps a process-local fallback and retries the shared store.

Every operational label is selected from a finite allowlist. User, asset, job,
archive, path, and exception values must never become metric labels.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import product
from threading import Lock
from typing import Iterable


HTTP_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "other")
TASK_NAMES = (
    "backup_operational_state",
    "cleanup_account_deletion",
    "dispatch_account_deletions",
    "group_media",
    "index_asset",
    "migrate_legacy_media",
    "monitor_storage",
    "process_asset",
    "protect_asset",
    "purge_expired_assets",
    "rebalance_user_storage",
    "restore_asset",
    "scan_library",
    "other",
)
TASK_OUTCOMES = ("failure", "retry", "revoked", "success")
QUEUE_NAMES = ("backup", "intelligence", "lifecycle", "media", "recovery", "storage", "other")
QUEUE_OUTCOMES = ("failed", "published")
STORAGE_MODES = ("scheduled_repair", "verify_and_repair", "verify_only")
STORAGE_RUN_OUTCOMES = ("failure", "success")
STORAGE_RESULTS = ("healthy", "recoverable", "replica_degraded", "unrecoverable")
BACKUP_KINDS = ("configuration", "database", "other")
BACKUP_OUTCOMES = ("created", "failed", "pruned", "reused", "skipped", "verified")
RECOVERY_OPERATIONS = ("asset_restore", "evidence_verification", "storage_verification")
RECOVERY_OUTCOMES = ("failed", "queued", "skipped", "success", "unrecoverable")

_TASK_QUEUE = {
    "backup_operational_state": "backup",
    "cleanup_account_deletion": "lifecycle",
    "dispatch_account_deletions": "lifecycle",
    "group_media": "intelligence",
    "index_asset": "intelligence",
    "migrate_legacy_media": "lifecycle",
    "monitor_storage": "storage",
    "process_asset": "media",
    "protect_asset": "storage",
    "purge_expired_assets": "lifecycle",
    "rebalance_user_storage": "storage",
    "restore_asset": "recovery",
    "scan_library": "media",
}


def bounded_task_name(value: object) -> str:
    candidate = str(getattr(value, "name", value) or "").rsplit(".", 1)[-1]
    return candidate if candidate in TASK_NAMES else "other"


def bounded_queue_name(value: object) -> str:
    return _TASK_QUEUE.get(bounded_task_name(value), "other")


def _bounded(value: object, allowed: tuple[str, ...], fallback: str) -> str:
    candidate = str(value or "")
    return candidate if candidate in allowed else fallback


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # These fields are deliberately bounded operational dimensions or
        # correlation values. Never add raw media paths, tokens, or payloads.
        for attribute in (
            "request_id",
            "traceparent",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "component",
            "operation",
            "outcome",
        ):
            value = getattr(record, attribute, None)
            if value is not None:
                payload[attribute] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(json_output: bool) -> None:
    if not json_output:
        return
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())


@dataclass(frozen=True)
class MetricSpec:
    help: str
    kind: str
    labels: tuple[tuple[str, tuple[str, ...]], ...] = ()


COUNTER_SPECS: dict[str, MetricSpec] = {
    "drivebound_worker_tasks_total": MetricSpec(
        "Celery task completions by bounded task name and outcome.",
        "counter",
        (("task", TASK_NAMES), ("outcome", TASK_OUTCOMES)),
    ),
    "drivebound_worker_task_duration_seconds_total": MetricSpec(
        "Total Celery execution time by bounded task name.", "counter", (("task", TASK_NAMES),)
    ),
    "drivebound_worker_task_executions_total": MetricSpec(
        "Celery executions included in the task duration total.", "counter", (("task", TASK_NAMES),)
    ),
    "drivebound_queue_publish_total": MetricSpec(
        "Task publish attempts by bounded queue family and outcome.",
        "counter",
        (("queue", QUEUE_NAMES), ("outcome", QUEUE_OUTCOMES)),
    ),
    "drivebound_queue_wait_duration_seconds_total": MetricSpec(
        "Total broker wait time observed when tasks start.", "counter", (("queue", QUEUE_NAMES),)
    ),
    "drivebound_queue_started_total": MetricSpec(
        "Tasks started after a measurable broker wait.", "counter", (("queue", QUEUE_NAMES),)
    ),
    "drivebound_storage_verification_runs_total": MetricSpec(
        "Storage verification runs by fixed mode and outcome.",
        "counter",
        (("mode", STORAGE_MODES), ("outcome", STORAGE_RUN_OUTCOMES)),
    ),
    "drivebound_storage_assets_verified_total": MetricSpec(
        "Assets classified by completed storage verification runs.",
        "counter",
        (("result", STORAGE_RESULTS),),
    ),
    "drivebound_backup_operations_total": MetricSpec(
        "Operational backup actions by archive kind and outcome.",
        "counter",
        (("kind", BACKUP_KINDS), ("outcome", BACKUP_OUTCOMES)),
    ),
    "drivebound_recovery_operations_total": MetricSpec(
        "Recovery actions by fixed operation and outcome.",
        "counter",
        (("operation", RECOVERY_OPERATIONS), ("outcome", RECOVERY_OUTCOMES)),
    ),
}

GAUGE_SPECS: dict[str, MetricSpec] = {
    "drivebound_worker_last_heartbeat_unixtime": MetricSpec(
        "Unix time of the most recent Celery worker heartbeat.", "gauge"
    ),
    "drivebound_worker_task_last_success_unixtime": MetricSpec(
        "Unix time of the most recent successful task by bounded task name.",
        "gauge",
        (("task", TASK_NAMES),),
    ),
    "drivebound_backup_last_verified_unixtime": MetricSpec(
        "Unix time of the most recent verified operational backup by kind.",
        "gauge",
        (("kind", BACKUP_KINDS),),
    ),
    "drivebound_storage_verification_last_success_unixtime": MetricSpec(
        "Unix time of the most recent successful storage verification by mode.",
        "gauge",
        (("mode", STORAGE_MODES),),
    ),
    "drivebound_recovery_evidence_last_success_unixtime": MetricSpec(
        "Unix time of the most recently published successful structural recovery-input verification.", "gauge"
    ),
}


def _label_values(spec: MetricSpec) -> Iterable[tuple[str, ...]]:
    if not spec.labels:
        yield ()
        return
    yield from product(*(allowed for _name, allowed in spec.labels))


def _field(metric_name: str, labels: tuple[str, ...]) -> str:
    # Names and values are finite internal constants, so this is reversible and
    # cannot be influenced by an asset/user/job identifier.
    return "|".join((metric_name, *labels))


def _escape_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class OperationalMetrics:
    """Finite-cardinality counters/gauges with an optional Redis mirror."""

    _COUNTER_KEY = "drivebound:metrics:v1:counters"
    _GAUGE_KEY = "drivebound:metrics:v1:gauges"

    def __init__(self, *, shared: bool = True) -> None:
        self._lock = Lock()
        self._shared = shared
        self._redis_client: object | None = None
        self._redis_retry_at = 0.0
        self._counters = {
            _field(name, values): 0.0
            for name, spec in COUNTER_SPECS.items()
            for values in _label_values(spec)
        }
        self._gauges = {
            _field(name, values): 0.0
            for name, spec in GAUGE_SPECS.items()
            for values in _label_values(spec)
        }

    def _client(self) -> object | None:
        if not self._shared or time.monotonic() < self._redis_retry_at:
            return None
        if self._redis_client is not None:
            return self._redis_client
        try:
            from redis import Redis

            from app.core.config import settings

            self._redis_client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.1,
                socket_timeout=0.1,
            )
            return self._redis_client
        except Exception:
            self._redis_retry_at = time.monotonic() + 5
            return None

    def _shared_failure(self) -> None:
        client, self._redis_client = self._redis_client, None
        self._redis_retry_at = time.monotonic() + 5
        try:
            if client is not None:
                client.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    def _increment(self, metric: str, labels: tuple[str, ...], amount: float = 1.0) -> bool:
        field = _field(metric, labels)
        with self._lock:
            self._counters[field] += amount
        client = self._client()
        if client is not None:
            try:
                client.hincrbyfloat(self._COUNTER_KEY, field, amount)  # type: ignore[attr-defined]
                return True
            except Exception:
                self._shared_failure()
        return False

    def _set(self, metric: str, labels: tuple[str, ...], value: float) -> bool:
        field = _field(metric, labels)
        with self._lock:
            self._gauges[field] = value
        client = self._client()
        if client is not None:
            try:
                client.hset(self._GAUGE_KEY, field, value)  # type: ignore[attr-defined]
                return True
            except Exception:
                self._shared_failure()
        return False

    def observe_worker(self, task: object, outcome: object, duration_seconds: float) -> None:
        task_name = bounded_task_name(task)
        bounded_outcome = _bounded(outcome, TASK_OUTCOMES, "failure")
        duration = max(float(duration_seconds), 0.0)
        self._increment("drivebound_worker_tasks_total", (task_name, bounded_outcome))
        self._increment("drivebound_worker_task_duration_seconds_total", (task_name,), duration)
        self._increment("drivebound_worker_task_executions_total", (task_name,))
        if bounded_outcome == "success":
            self._set("drivebound_worker_task_last_success_unixtime", (task_name,), time.time())

    def observe_queue_publish(self, task: object, outcome: object = "published") -> None:
        queue = bounded_queue_name(task)
        bounded_outcome = _bounded(outcome, QUEUE_OUTCOMES, "failed")
        self._increment("drivebound_queue_publish_total", (queue, bounded_outcome))

    def observe_queue_wait(self, task: object, duration_seconds: float) -> None:
        queue = bounded_queue_name(task)
        self._increment("drivebound_queue_wait_duration_seconds_total", (queue,), max(float(duration_seconds), 0.0))
        self._increment("drivebound_queue_started_total", (queue,))

    def observe_storage_run(self, mode: object, outcome: object, results: Counter[str] | None = None) -> None:
        bounded_mode = _bounded(mode, STORAGE_MODES, "scheduled_repair")
        bounded_outcome = _bounded(outcome, STORAGE_RUN_OUTCOMES, "failure")
        self._increment("drivebound_storage_verification_runs_total", (bounded_mode, bounded_outcome))
        if bounded_outcome == "success":
            self._set("drivebound_storage_verification_last_success_unixtime", (bounded_mode,), time.time())
            for result, count in (results or {}).items():
                bounded_result = _bounded(result, STORAGE_RESULTS, "unrecoverable")
                if count > 0:
                    self._increment("drivebound_storage_assets_verified_total", (bounded_result,), float(count))

    def observe_backup(self, kind: object, outcome: object) -> None:
        bounded_kind = _bounded(kind, BACKUP_KINDS, "other")
        bounded_outcome = _bounded(outcome, BACKUP_OUTCOMES, "failed")
        self._increment("drivebound_backup_operations_total", (bounded_kind, bounded_outcome))
        if bounded_outcome == "verified":
            self._set("drivebound_backup_last_verified_unixtime", (bounded_kind,), time.time())

    def set_backup_last_verified(self, kind: object, timestamp: float) -> None:
        bounded_kind = _bounded(kind, BACKUP_KINDS, "other")
        self._set("drivebound_backup_last_verified_unixtime", (bounded_kind,), max(float(timestamp), 0.0))

    def observe_recovery(self, operation: object, outcome: object) -> bool:
        bounded_operation = _bounded(operation, RECOVERY_OPERATIONS, "storage_verification")
        bounded_outcome = _bounded(outcome, RECOVERY_OUTCOMES, "failed")
        shared = self._increment("drivebound_recovery_operations_total", (bounded_operation, bounded_outcome))
        if bounded_operation == "evidence_verification" and bounded_outcome == "success":
            shared = self._set("drivebound_recovery_evidence_last_success_unixtime", (), time.time()) and shared
        return shared

    def worker_heartbeat(self) -> None:
        self._set("drivebound_worker_last_heartbeat_unixtime", (), time.time())

    def _snapshot(self) -> tuple[dict[str, float], dict[str, float]]:
        with self._lock:
            local_counters = dict(self._counters)
            local_gauges = dict(self._gauges)
        client = self._client()
        if client is None:
            return local_counters, local_gauges
        try:
            remote_counters = client.hgetall(self._COUNTER_KEY)  # type: ignore[attr-defined]
            remote_gauges = client.hgetall(self._GAUGE_KEY)  # type: ignore[attr-defined]
            return (
                {field: float(remote_counters.get(field, 0.0)) for field in local_counters},
                {field: float(remote_gauges.get(field, 0.0)) for field in local_gauges},
            )
        except Exception:
            self._shared_failure()
            return local_counters, local_gauges

    @staticmethod
    def _sample(name: str, spec: MetricSpec, values: tuple[str, ...], value: float) -> str:
        if spec.labels:
            labels = ",".join(
                f'{label_name}="{_escape_label(label_value)}"'
                for (label_name, _allowed), label_value in zip(spec.labels, values, strict=True)
            )
            return f"{name}{{{labels}}} {value:.6f}"
        return f"{name} {value:.6f}"

    def prometheus_lines(self) -> list[str]:
        counters, gauges = self._snapshot()
        lines: list[str] = []
        for specs, values_by_field in ((COUNTER_SPECS, counters), (GAUGE_SPECS, gauges)):
            for name, spec in specs.items():
                lines.extend((f"# HELP {name} {spec.help}", f"# TYPE {name} {spec.kind}"))
                for values in _label_values(spec):
                    lines.append(self._sample(name, spec, values, values_by_field[_field(name, values)]))
        return lines


class RequestMetrics:
    def __init__(self, *, shared_operational: bool = True) -> None:
        self._lock = Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._latency_seconds: defaultdict[tuple[str, str], float] = defaultdict(float)
        self.operations = OperationalMetrics(shared=shared_operational)

    def observe(self, method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
        bounded_method = method.upper() if method.upper() in HTTP_METHODS else "other"
        bounded_status = status_code if 100 <= status_code <= 599 else 0
        with self._lock:
            self._requests[(bounded_method, path, bounded_status)] += 1
            self._latency_seconds[(bounded_method, path)] += elapsed_seconds

    def prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP drivebound_http_requests_total HTTP requests handled by this process.",
                "# TYPE drivebound_http_requests_total counter",
            ]
            for (method, path, status), count in sorted(self._requests.items()):
                lines.append(
                    "drivebound_http_requests_total"
                    f'{{method="{_escape_label(method)}",path="{_escape_label(path)}",status="{status}"}} {count}'
                )
            lines.extend([
                "# HELP drivebound_http_request_duration_seconds_total Total HTTP request duration by route.",
                "# TYPE drivebound_http_request_duration_seconds_total counter",
            ])
            for (method, path), seconds in sorted(self._latency_seconds.items()):
                lines.append(
                    "drivebound_http_request_duration_seconds_total"
                    f'{{method="{_escape_label(method)}",path="{_escape_label(path)}"}} {seconds:.6f}'
                )
        lines.extend(self.operations.prometheus_lines())
        return "\n".join(lines) + "\n"


metrics = RequestMetrics()
request_logger = logging.getLogger("drivebound.request")
operation_logger = logging.getLogger("drivebound.operation")


def log_operation(*, component: str, operation: str, outcome: str, duration_seconds: float | None = None) -> None:
    extra: dict[str, object] = {"component": component, "operation": operation, "outcome": outcome}
    if duration_seconds is not None:
        extra["duration_ms"] = round(max(duration_seconds, 0) * 1000, 2)
    operation_logger.info("operation_complete", extra=extra)


def log_request(*, request_id: str, traceparent: str | None, method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
    request_logger.info(
        "request_complete",
        extra={
            "request_id": request_id,
            "traceparent": traceparent,
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round(elapsed_seconds * 1000, 2),
        },
    )


def request_timer() -> float:
    return time.perf_counter()
