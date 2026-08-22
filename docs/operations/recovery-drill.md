# Recovery drill runbook

This runbook has two deliberately separate controls. Automated structural
evidence verifies that approved backup inputs remain intact without restoring
anything. Only the isolated full-restore operator gate proves that Drivebound
can recover metadata and media. Never describe a passing verify-only report as
a successful restore drill.

## Required recovery set

- A recently verified `database` archive from `BACKUPS_PATH`.
- A recent `configuration` archive from the same directory.
- The independently retained `account-deletion-suppressions` ledger that was
  current when the recovery inputs were captured.
- Separately protected deployment secrets, especially the PostgreSQL password,
  `JWT_SECRET`, `MFA_ENCRYPTION_SECRET`, and
  `MEDIA_ENCRYPTION_MASTER_KEY`.
- At least one checksum-verified media replica on a different physical device.
- The exact Drivebound release or immutable image digest being restored.

Configuration archives deliberately contain no secrets. A database archive and
replica bytes are not sufficient without the media-encryption master key. A
historical database must never be exposed to traffic without the matching
suppression ledger: it is what lets startup reapply deletion intent that is
newer than the restored dump.

## Automated structural evidence (not a restore drill)

Before application-level checks, create an operator-owned manifest whose paths
are relative to the manifest or absolute. Use expected SHA-256 values captured
when each approved recovery input was created:

```json
{
  "schema": 1,
  "evidence_id": "monthly-2026-08",
  "release": "v0.5.0",
  "database": {"path": "database.dump", "sha256": "<64 lowercase hex>"},
  "configuration": {"path": "configuration.json", "sha256": "<64 lowercase hex>"},
  "media_samples": [
    {"path": "sample-original.enc", "sha256": "<64 lowercase hex>"}
  ]
}
```

For a one-off check, run the verify-only command from the repository root:

```powershell
python tools/recovery_drill.py recovery-manifest.json --report-dir D:\DriveboundRecoveryEvidence --publish-metric
```

The command checks every checksum, runs `pg_restore --list` against the logical
archive, validates that the configuration snapshot is structurally valid and
secret-free, and verifies that input size/timestamps did not change during the
read. It never connects to PostgreSQL, restores data, or writes media. The
redacted report excludes absolute paths, filenames, and checksums. It explicitly
records `evidence_scope=structural_recovery_inputs_only`,
`isolated_restore_attempted=false`, and `application_recovery_proven=false`.
`--report-dir` creates a mode-0600, create-exclusive timestamped file and never
prunes or overwrites prior evidence. `--publish-metric` is optional; it publishes
only the aggregate pass/fail result to the configured metrics Redis store. Exit
status is `0` for fully passing structural evidence, `1` for failed checks, and
`2` for an invalid manifest, report-writing error, or an explicitly requested
metrics publish that Redis did not accept. The JSON report is still retained
when only metric publication fails.

### Scheduler and CI contract

Use `tools/run_recovery_evidence.py` for Task Scheduler, cron, or a CI runner
with operator-approved recovery inputs mounted read-only and a separate durable
report directory mounted writable. Supply values by arguments:

```powershell
python tools/run_recovery_evidence.py --manifest D:\RecoveryInputs\manifest.json --report-dir D:\DriveboundRecoveryEvidence --publish-metric
```

Or supply `DRIVEBOUND_RECOVERY_MANIFEST`, `DRIVEBOUND_RECOVERY_REPORT_DIR`, and
optionally `DRIVEBOUND_RECOVERY_PUBLISH_METRIC=true`. The wrapper uses the
current Python executable, does not invoke a shell, always selects timestamped
report-directory mode, and returns the verifier's exact `0`/`1`/`2` status.

Run this control at least monthly. The scheduler must alert on every nonzero
exit, keep the report directory outside the backup-input mount, restrict it to
operators, and retain its append-only timestamped JSON reports for at least 13
months. Enable metric publication when the scheduler can reach the dedicated
Redis instance; `DriveboundRecoveryEvidenceOverdue` then alerts after 35 days
without passing structural evidence. CI using synthetic fixtures validates the
automation itself but is not evidence about production recovery inputs.

## Monthly authenticated non-destructive verification

1. Sign in to the staging account and inspect `GET /api/v1/storage/recovery`.
   Record the latest verified database/configuration timestamps, failed archive
   count, and the reported `recoverable` state.
2. Queue `POST /api/v1/monitoring/verify`. This checks originals and protection
   copies for the signed-in account without modifying media.
3. Review `GET /api/v1/monitoring` until the scan completes. Investigate every
   open corruption, missing-original, or missing-replica event.
4. Only when repair is intended, queue `POST /api/v1/monitoring/run`. Drivebound
   may rebuild a replica or atomically restore a managed original from a valid
   copy; it never deletes an external-library original.
5. Restore one representative original to a phone or download it through the
   web viewer. Verify the SHA-256 checksum, capture date, filesystem timestamp,
   GPS/camera metadata, thumbnail, and video playback where applicable.

## Required isolated full-restore operator gate

A passing structural-evidence report does not satisfy this gate. Perform these
steps in an isolated environment with a distinct database server/name and
disposable media copies. The resulting operator record must capture restored
application behavior, not merely archive readability.

1. Copy a verified archive to a staging host. Do not mount the production
   database volume and do not target the live database hostname.
2. Use `pg_restore --list <archive>` once more before restore.
3. Create a new database with a drill-specific name such as
   `drivebound_restore_drill_20260815`. Confirm that the resolved server and
   database name are the isolated targets before running `pg_restore`.
4. Apply the archive to that empty database. Restore the matching suppression
   ledger and media-encryption master key before starting any API process. Keep
   the environment isolated from public ingress, then start exactly one API;
   startup must reconcile the ledger before it can accept traffic. Do not start
   workers until schema and suppression-job inspection succeed.
5. Confirm Alembic is at the expected revision and verify every suppression
   marker that matches a restored account recreated or preserved a deletion
   job and left that account disabled. Then verify account login,
   library ownership boundaries, timeline counts, album membership, Trash and
   version history, replica records, and preview decryption.
6. Start one worker and the deletion dispatcher. Allow replayed deletion jobs
   to reach a terminal state against disposable media, then run the
   non-destructive account verification endpoint. Exercise repair only against
   disposable copies created for the drill.
7. Record elapsed restore time, archive age, missing prerequisites, checksum
   results, and the release/image identifiers used.
8. Destroy the isolated drill database and decrypted temporary data according
   to the staging retention policy after the report has been retained.

## Failure handling

- Never mark a drill successful based only on `pg_restore --list`; that proves
  archive structure, not a usable application restore.
- If every replica for an asset fails checksum verification, keep the database
  record and monitoring event. Do not replace it with an unverified candidate.
- If the media-encryption key is missing, stop. Rotating unrelated secrets does
  not make encrypted originals recoverable.
- If a backup is stale or failed, preserve it for investigation and create a
  new verified archive before pruning anything.
- If the suppression ledger is absent, invalid, or cannot be paired with the
  recovery set and media key, keep ingress closed. Recover the ledger from its
  independent copy; do not infer deletion state from the older database alone.
- Treat a downgrade that would reinstate older uniqueness constraints as a data
  migration, not as an automatic rollback.

The drill is complete only when login, metadata, decrypted media, ownership
isolation, and at least one real restore path have all been demonstrated.
