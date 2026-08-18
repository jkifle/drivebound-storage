# Production observability

Drivebound exposes Prometheus text at `/metrics`. In remote deployments this
endpoint requires the configured bearer token and should also be reachable only
from the scraper network. API request series remain process-local. Operational
worker, queue, storage, backup, and recovery series are mirrored through the
configured Redis database, so the API scrape includes work completed by Celery.
If Redis is unavailable, product work continues and each process keeps a local
metrics fallback; expect a telemetry gap until Redis recovers.

Load [`alerts/drivebound.rules.yml`](alerts/drivebound.rules.yml) into the
Prometheus-compatible rule evaluator. The default thresholds assume a 15-minute
storage-monitor schedule and daily database backups. Adjust the time thresholds
when those schedules are intentionally changed, but do not add user, asset,
archive, job, path, error-message, or worker-host labels.

## Cardinality contract

Operational series accept only finite labels compiled into
`app.core.observability`. Unknown task names and archive kinds collapse to
`other`; unknown outcomes collapse to a safe failure outcome. Queue metrics use
seven functional families rather than broker routing keys. This makes externally
controlled IDs unable to create Prometheus series.

The shared Redis keys are `drivebound:metrics:v1:counters` and
`drivebound:metrics:v1:gauges`. They contain only metric names, bounded labels,
and numeric values. Reset them only during a planned monitoring reset; deleting
them loses counters and makes freshness alerts fire until healthy work runs.

## API and worker availability

- Confirm the API target is reachable from the scraper with the metrics bearer
  token, then inspect API logs by `X-Request-ID` or `traceparent`.
- A stale worker heartbeat usually means Celery is stopped, cannot reach Redis,
  or the API scraper cannot read the shared operational snapshot.
- Restarting Redis resets shared telemetry unless persistence is enabled. Treat
  alerts immediately after an intentional reset as requiring fresh backup and
  storage-verification runs, not manual gauge editing.

## Queue and task failures

- `drivebound_queue_publish_total{outcome="failed"}` covers publish failures
  observed at guarded queue boundaries. `outcome="published"` is emitted by the
  Celery publish signal.
- Queue wait is measured from a non-sensitive publish timestamp carried in the
  task header to task start. No task ID or arguments are exported.
- Inspect bounded structured worker logs (`component`, `operation`, `outcome`,
  and `duration_ms`) for the failing task family. Do not log task arguments.

## Storage and recovery

`verify_only`, `verify_and_repair`, and `scheduled_repair` are distinct storage
run modes. Asset results are aggregated as `healthy`, `replica_degraded`,
`recoverable`, or `unrecoverable`; no account or asset identity is exported.
An unrecoverable result requires operator investigation before any pruning.
Use the authenticated monitoring screen for account-scoped detail, then follow
the recovery runbook. Never turn a verification alert into an automatic delete.

## Backups and structural recovery evidence

Backup freshness advances only after checksum and archive-format verification.
The `drivebound_recovery_evidence_last_success_unixtime` gauge advances only
when structural recovery-input verification is run with `--publish-metric`;
this explicit flag writes the aggregate outcome to metrics Redis and does not
publish paths, checksums, or evidence identifiers. The command returns status
`2` if Redis does not accept that explicit publication. Timestamped redacted
reports are retained separately as audit artifacts.

This gauge proves only that approved input files remained checksum-valid and
structurally readable. It does not prove PostgreSQL restoration, application
startup, authentication, decryption, ownership isolation, or media playback.
Those claims require the isolated full-restore operator gate in the recovery
runbook.
