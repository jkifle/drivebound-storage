import time
import uuid
from types import SimpleNamespace

import pytest
from celery.app.task import Task

from app.core.observability import (
    BACKUP_KINDS,
    BACKUP_OUTCOMES,
    QUEUE_NAMES,
    QUEUE_OUTCOMES,
    RECOVERY_OPERATIONS,
    RECOVERY_OUTCOMES,
    STORAGE_MODES,
    STORAGE_RESULTS,
    TASK_NAMES,
    TASK_OUTCOMES,
    OperationalMetrics,
    RequestMetrics,
)
from app.worker import celery_app as celery_instrumentation
from app.worker import tasks as worker_tasks  # noqa: F401 - registers every declared task


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, float]] = {}

    def hincrbyfloat(self, key: str, field: str, amount: float) -> None:
        values = self.hashes.setdefault(key, {})
        values[field] = values.get(field, 0.0) + amount

    def hset(self, key: str, field: str, value: float) -> None:
        self.hashes.setdefault(key, {})[field] = value

    def hgetall(self, key: str) -> dict[str, float]:
        return dict(self.hashes.get(key, {}))

    def close(self) -> None:
        pass


def test_operational_metrics_have_only_fixed_cardinality_labels() -> None:
    registry = RequestMetrics(shared_operational=False)
    resource_id = uuid.uuid4().hex

    registry.operations.observe_worker(f"unknown-{resource_id}", f"unknown-{resource_id}", 0.5)
    registry.operations.observe_queue_publish(f"unknown-{resource_id}", f"unknown-{resource_id}")
    registry.operations.observe_storage_run(f"unknown-{resource_id}", "success", {f"unknown-{resource_id}": 2})
    registry.operations.observe_backup(f"unknown-{resource_id}", f"unknown-{resource_id}")
    registry.operations.observe_recovery(f"unknown-{resource_id}", f"unknown-{resource_id}")
    output = registry.prometheus()

    assert resource_id not in output
    assert 'task="other",outcome="failure"' in output
    assert 'queue="other",outcome="failed"' in output
    assert 'kind="other",outcome="failed"' in output
    assert 'operation="storage_verification",outcome="failed"' in output
    assert output.count("drivebound_worker_tasks_total{") == len(TASK_NAMES) * len(TASK_OUTCOMES)
    assert output.count("drivebound_queue_publish_total{") == len(QUEUE_NAMES) * len(QUEUE_OUTCOMES)
    assert output.count("drivebound_backup_operations_total{") == len(BACKUP_KINDS) * len(BACKUP_OUTCOMES)
    assert output.count("drivebound_recovery_operations_total{") == len(RECOVERY_OPERATIONS) * len(RECOVERY_OUTCOMES)
    assert output.count("drivebound_storage_assets_verified_total{") == len(STORAGE_RESULTS)
    assert output.count("drivebound_storage_verification_runs_total{") == len(STORAGE_MODES) * 2


def test_shared_operational_snapshot_is_visible_to_another_registry() -> None:
    shared = FakeRedis()
    worker = OperationalMetrics()
    api = OperationalMetrics()
    worker._redis_client = shared
    api._redis_client = shared

    worker.observe_worker("monitor_storage", "success", 2.5)
    worker.observe_backup("database", "verified")
    output = "\n".join(api.prometheus_lines())

    assert 'drivebound_worker_tasks_total{task="monitor_storage",outcome="success"} 1.000000' in output
    assert 'drivebound_worker_task_duration_seconds_total{task="monitor_storage"} 2.500000' in output
    assert 'drivebound_backup_operations_total{kind="database",outcome="verified"} 1.000000' in output
    assert 'drivebound_backup_last_verified_unixtime{kind="database"} 0.000000' not in output


def test_celery_signals_carry_only_timing_and_bounded_task_names(monkeypatch) -> None:
    headers = {"argsrepr": "must-not-be-exported"}
    waits: list[tuple[object, float]] = []
    completions: list[tuple[object, object, float]] = []
    logs: list[dict[str, object]] = []
    monkeypatch.setattr(
        celery_instrumentation.metrics.operations,
        "observe_queue_wait",
        lambda task, duration: waits.append((task, duration)),
    )
    monkeypatch.setattr(
        celery_instrumentation.metrics.operations,
        "observe_worker",
        lambda task, outcome, duration: completions.append((task, outcome, duration)),
    )
    monkeypatch.setattr(celery_instrumentation, "log_operation", lambda **values: logs.append(values))

    celery_instrumentation.stamp_task_publish(headers=headers)
    assert set(headers) == {"argsrepr", "drivebound_published_at"}
    headers["drivebound_published_at"] = time.time() - 1
    task = SimpleNamespace(name="restore_asset", request=SimpleNamespace(headers=headers))
    celery_instrumentation.record_task_start(sender=task, task=task)
    celery_instrumentation.record_task_completion(sender=task, task=task, state="SUCCESS")

    assert waits[0][0] is task
    assert waits[0][1] >= 0
    assert completions[0][0:2] == ("restore_asset", "success")
    assert logs[0]["operation"] == "restore_asset"
    assert "argsrepr" not in logs[0]
    assert not hasattr(task.request, "_drivebound_started_at")


def test_task_timing_is_isolated_across_interleaved_request_contexts(monkeypatch) -> None:
    class ReusableTask:
        name = "restore_asset"

        def __init__(self) -> None:
            self.active_request = SimpleNamespace(headers={})

        @property
        def request(self):
            return self.active_request

    task = ReusableTask()
    request_a = SimpleNamespace(headers={})
    request_b = SimpleNamespace(headers={})
    clock = iter((10.0, 20.0, 24.0, 30.0))
    completions: list[float] = []
    monkeypatch.setattr(celery_instrumentation.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        celery_instrumentation.metrics.operations,
        "observe_worker",
        lambda _task, _outcome, duration: completions.append(duration),
    )
    monkeypatch.setattr(celery_instrumentation, "log_operation", lambda **_values: None)

    task.active_request = request_a
    celery_instrumentation.record_task_start(sender=task, task=task)
    task.active_request = request_b
    celery_instrumentation.record_task_start(sender=task, task=task)
    celery_instrumentation.record_task_completion(sender=task, task=task, state="SUCCESS")
    task.active_request = request_a
    celery_instrumentation.record_task_completion(sender=task, task=task, state="SUCCESS")

    assert completions == [4.0, 20.0]
    assert not hasattr(request_a, "_drivebound_started_at")
    assert not hasattr(request_b, "_drivebound_started_at")


def test_every_declared_task_base_records_broker_publish_failure(monkeypatch) -> None:
    failures: list[tuple[object, object]] = []
    monkeypatch.setattr(
        celery_instrumentation.metrics.operations,
        "observe_queue_publish",
        lambda task, outcome: failures.append((task, outcome)),
    )

    def fail_publish(_task, *_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(Task, "apply_async", fail_publish)

    declared = [name for name in TASK_NAMES if name != "other"]
    for name in declared:
        task = celery_instrumentation.celery_app.tasks[name]
        assert isinstance(task, celery_instrumentation.ObservableTask)
        with pytest.raises(RuntimeError, match="broker unavailable"):
            task.delay()

    assert failures == [(name, "failed") for name in declared]


def test_http_method_and_status_dimensions_are_bounded() -> None:
    registry = RequestMetrics(shared_operational=False)
    registry.observe("INVENTED", "/unmatched", 999, 0.1)

    output = registry.prometheus()

    assert 'method="other",path="/unmatched",status="0"' in output
    assert "INVENTED" not in output
    assert "999" not in output
