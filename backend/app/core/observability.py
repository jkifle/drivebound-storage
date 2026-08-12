"""Low-cost observability primitives for local and container deployments.

Logs are structured JSON when requested, while metrics use the Prometheus text
format so any managed or self-hosted scraper can collect them. Request IDs and
W3C ``traceparent`` values are preserved for cross-service correlation.
"""

import json
import logging
import time
from collections import Counter, defaultdict
from threading import Lock


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attribute in ("request_id", "traceparent", "method", "path", "status_code", "duration_ms"):
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


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._latency_seconds: defaultdict[tuple[str, str], float] = defaultdict(float)

    def observe(self, method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
        with self._lock:
            self._requests[(method, path, status_code)] += 1
            self._latency_seconds[(method, path)] += elapsed_seconds

    def prometheus(self) -> str:
        with self._lock:
            lines = [
                "# HELP drivebound_http_requests_total HTTP requests handled by this process.",
                "# TYPE drivebound_http_requests_total counter",
            ]
            for (method, path, status), count in sorted(self._requests.items()):
                lines.append(f'drivebound_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}')
            lines.extend([
                "# HELP drivebound_http_request_duration_seconds_total Total HTTP request duration by route.",
                "# TYPE drivebound_http_request_duration_seconds_total counter",
            ])
            for (method, path), seconds in sorted(self._latency_seconds.items()):
                lines.append(f'drivebound_http_request_duration_seconds_total{{method="{method}",path="{path}"}} {seconds:.6f}')
        return "\n".join(lines) + "\n"


metrics = RequestMetrics()
request_logger = logging.getLogger("drivebound.request")


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
