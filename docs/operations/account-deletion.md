# Durable account deletion

Drivebound account deletion is an irreversible, two-stage workflow. The API
first locks the account row and authenticates the destructive request. In one
database transaction it disables and anonymizes the user, destroys the live
wrapped media key, removes every session and alternate credential, revokes
public shares and collaboration capabilities, marks all asset revisions for
purge, and creates one durable `account_deletion_jobs` record. The browser
receives `202 Accepted` only after an independent suppression marker has been
written under `BACKUPS_PATH/account-deletion-suppressions`.

The marker itself is an authorization denylist. Bearer and refresh sessions,
new session issuance, mobile-device credentials, paired-node credentials,
public shares, and collaborative album/media reads all consult it. Access is
therefore denied as soon as the marker is durable even if the following
PostgreSQL commit has an uncertain outcome. A Celery worker then removes resumable-upload staging files,
managed originals, encrypted derivatives, protection replicas and their
sidecars one item per commit. It uses a leased job, exponential retry state,
path validation, canonical path advisory locks, and cross-kind reference
checks. A worker crash or duplicate delivery is safe. The final User row is
deleted only after the catalog is empty and the worker has swept unreferenced
files from the account-scoped encrypted namespaces.

External-library originals are never removed. A byte that is still referenced
by another account/path column is retained. Historical noncanonical aliases
and link/junction escapes are also retained rather than guessed at. Their
catalog evidence remains attached to the disabled subject and the job enters
`manual_review`; it is not reported as completed. The bounded
`retained_legacy_paths` counter supports alerting without copying filenames
into job/audit detail.

## Restore suppression

Logical database archives created before deletion still contain the old
wrapped key and account metadata. The suppression ledger is therefore part of
the backup set, not disposable cache. Each marker contains a keyed-HMAC subject
fingerprint and an authentication tag derived from the stable, file-backed
media-encryption master key; it contains no email or raw user UUID.

The API replays this ledger synchronously during startup before accepting
traffic. Restoring PostgreSQL without the matching suppression directory, or
with a different media master key, is not a valid recovery. Preserve and mount
the ledger before starting a restored API. Remote, staging, and production
startup fails if this directory is missing; pre-create the durable mounted
directory during the initial rollout. Local development may initialize it
automatically. Markers are never pruned
automatically: `suppression_review_after` is only the earliest review date.
Remove a marker only after verified inventory proves that no local, off-site,
legal-hold, or investigative archive predating the deletion remains.

## Deployment and rollback gate

This feature requires migration `0017` and the matching API and worker code.
Use this rollout order:

1. Back up and migrate PostgreSQL through `0017`.
2. Deploy the new API, scheduler, and workers with the same durable originals,
   derivatives, staging, replica, backup-ledger, and media-key mounts.
   Create `BACKUPS_PATH/account-deletion-suppressions` on the durable ledger
   mount before starting a public API. Configure every external-library root
   on a distinct mount/path that neither contains nor is contained by any
   originals, derivatives, staging, backups, or replica root.
3. Drain and stop every pre-0017 worker. Old workers do not participate in the
   User/path locks and may recreate bytes after cleanup.
4. Start the new workers and scheduler, verify their heartbeat, then expose the
   account-deletion action.

Downgrade from `0017` aborts while any deletion-job evidence exists. Before a
rollback, finish or investigate all jobs, preserve/reconcile the independent
suppression ledger, drain new workers, and only then explicitly archive/remove
the job rows. Never bypass the downgrade guard on a live system.

## Operational states

`pending` and `retry` jobs are publishable outbox entries; `queued` jobs are
republished after their visibility deadline if no worker claims them;
`running` jobs have a fenced lease; `manual_review` jobs remain disabled and
retain their catalog evidence until an operator resolves unsafe legacy paths;
and `completed` jobs retain only bounded aggregate evidence. A delayed worker
or publish callback cannot reclaim or overwrite `manual_review`. The scheduler publishes `dispatch_account_deletions`
every `ACCOUNT_DELETION_DISPATCH_INTERVAL_SECONDS`. Cleanup batches contain at
most `ACCOUNT_DELETION_BATCH_SIZE` items and renew their lease after each
committed item. Broker publish failure never discards the database job.

Treat a sustained `retry` or `manual_review` state, expired lease, nonzero
`retained_legacy_paths`, missing suppression-ledger mount, overlapping external
and managed storage roots, or any startup
ledger-authentication failure as an operator incident. Never re-enable a
disabled deletion subject with `tools/account_recovery.py`; the tool refuses
both database tombstones and independently retained suppression markers.
