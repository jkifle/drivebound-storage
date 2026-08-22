# Drivebound production advancement roadmap

Last reconciled with the repository on 2026-08-22. Drivebound is in late
Tranche A and partial Tranche B: daily-use behavior is present at the
application layer, while production security, recovery, deletion, mobile, and
operator acceptance are not all closed. This document separates
delivered behavior from operator or environment acceptance. **Implemented**
means the capability is present and has local automated evidence. **Partial**
means useful implementation is present, but a required integration, deployment,
or recovery gate has not yet been demonstrated.

Local automated suites cover mobile contracts, lifecycle and sync, node
attestation, observability, operational metrics, structural recovery evidence,
authentication hardening, and durable account deletion. The current local
baseline is 161 passing backend tests with one platform-dependent link test
skipped, a passing frontend lint and production build, a passing mobile
typecheck, and clean offline upgrade/downgrade SQL through the single `0017`
migration head. Local tests are not evidence of a live isolated restore,
deletion replay from a historical archive, complete physical erasure, a
real-authenticator WebAuthn browser matrix, physical-device/app-store delivery,
production alert delivery, or a hosted relay deployment.

## Tranche A - reliable daily use

**Status: late; core workflows are implemented with local automated evidence,
while physical-device, real-drive, and restore acceptance remain external
gates.**

- The native client uses a durable, device-scoped SQLite transfer queue with
  resumable offsets, bounded retry, expired-lease recovery, slow-transfer lease
  heartbeats, pause/cancel/retry controls, and Wi-Fi, charging, schedule, and
  bandwidth policies. Production connections require HTTPS.
- The mobile library includes paginated browsing, search, albums, memories,
  location discovery, sharing, playback, original restore-to-device, and
  background-backup registration. Original bytes, filenames, filesystem dates,
  capture time, EXIF, GPS, and video-container metadata are retained through
  ingestion.
- Assets have immutable revisions, active-version rollback, retained Trash,
  restore, audited purge, and byte-reference safeguards. The web library exposes
  Trash and revision-history workflows.
- Scheduled configuration snapshots and logical PostgreSQL archives include
  checksums, archive-format verification, retention, and an authenticated,
  path-redacted recovery-readiness summary. Deployment still has to supply
  `pg_dump`/`pg_restore`, durable backup storage, and a monitored schedule.

## Tranche B - production security and operations

**Status: partial; most application controls are implemented, while production
acceptance remains.**

- **Implemented and locally validated:** authenticated metrics for remote
  deployments; route-safe, fixed-cardinality API, worker, queue, storage,
  backup, and recovery series; Redis-backed cross-process aggregation; bounded
  structured logs; and Prometheus alert rules.
- **Implemented and locally validated:** non-destructive account-scoped storage
  verification and repair controls, redacted structural recovery-input evidence,
  a scheduler/CI wrapper, append-only reports, and bounded failure-injection
  fixtures.
- **Implemented and locally validated:** WebAuthn/passkey
  sign-in and credential management, password/passkey/Google reauthentication,
  recent-authentication enforcement for high-risk account actions, and
  capability-aware sign-in and profile journeys. The backend tests and frontend
  production build pass; real Google OAuth and the physical-authenticator
  browser matrix remain release gates.
- **Implemented and locally validated:** Ed25519 node attestation, replay and
  freshness checks, legacy-node repair, and opaque node-secret rotation.
  The supported node is the dependency-free `tools/drivebound_node.py` client;
  the incompatible experimental standalone `node_service` package was removed.
  Signed node software updates and identity-key rotation are not implemented.
- **Implemented and locally validated; operator acceptance remains:** migration `0017`
  adds durable, leased, retrying account-deletion jobs and an independent HMAC
  suppression ledger replayed before API traffic after a restore. Live access,
  credentials, and the live media key are destroyed at acceptance; catalog-owned
  managed bytes are then removed asynchronously. Canonical cross-kind reference
  protection, account-scoped orphan and staging sweeps, writer fences, and a
  retained `manual_review` state are implemented. Historical archives are not
  instantly erased and external-library originals remain read-only. Production
  still needs a live ledger-replay/deletion drill and retained operator evidence.
  See the [account-deletion runbook](../operations/account-deletion.md).
- **Partial:** the recovery runbook and automation deliberately prove only that
  approved inputs are checksum-valid and structurally readable. No isolated
  PostgreSQL restore, application startup, ownership check, media decryption, or
  playback recovery has been demonstrated by that evidence. The mandatory
  isolated full-restore operator gate remains open.
- **Partial:** Android/iOS release profiles, production HTTPS gates, and privacy
  guidance are present. Real signing credentials, physical-device release
  acceptance, and store delivery remain.
- **Pending deployment work:** route alerts to an operator-owned destination,
  exercise them against staging failures, configure production secrets/TLS and
  durable metrics storage, and record the first recovery and release evidence.

## Next bounded production tranche - acceptance closure

Complete this tranche before adding more user-facing scope:

1. Run the locally validated authentication and deletion contracts against the
   release environment: live PostgreSQL and Redis/Celery, real Google OAuth and
   SMTP, broker failure, worker restart, filesystem outage, suppression-ledger
   replay, legacy paths, and orphan-byte inventory. Retain the resulting
   password, MFA, recovery, revocation, export, and deletion evidence.
2. Run the WebAuthn flows with real platform and roaming authenticators across
   the supported browser/device matrix, including cancellation, expired
   challenges, lost credentials, and account-recovery fallback.
3. Execute the isolated full-restore gate with a recent database archive,
   configuration snapshot, protected media sample, restored secrets, and an
   immutable release image. Record RTO/RPO, ownership, decryption, playback, and
   cleanup evidence.
4. Connect the shipped alert rules to the production notification path and
   stage controlled worker, queue, storage, backup, and recovery-evidence
   failures to prove delivery and runbook ownership.
5. Produce one signed mobile release candidate, validate background backup and
   restore on physical Android and iOS devices, and retain the resulting release
   checklist.

Exit only when the evidence above is retained and every failed check has an
owner. The next implementation tranche after acceptance closure is signed node
update manifests plus safe identity-key rotation.

## Tranche C - hosted connectivity

**Status: planned.**

- Deploy the hosted pairing/control plane independently from customer storage.
- Add an encrypted node relay with direct-connect preference and NAT traversal.
- Add offline command journals, idempotent replay, and regional relay failover.
- Add household/workspace tenancy, quotas, support tooling, and billing
  boundaries.
- Define and enforce data-residency and deletion guarantees for hosted metadata.

## Tranche D - private media intelligence

**Status: planned.**

- Add user-controlled face clustering and naming with local deletion controls.
- Add object, pet, landmark, event, and speech indexing.
- Add reverse geocoding and private place labels.
- Add similar-media discovery, automatic album suggestions, and richer memories.
- Add per-feature consent, derived-data export, reindex, and deletion workflows.

## Tranche E - richer media workflows

**Status: planned.**

- Add RAW/JPEG and Live Photo playback/editing workflows.
- Add non-destructive image edits and video trim metadata.
- Add adaptive video previews and casting.
- Add guest contribution links, comments, reactions, and approval workflows.
- Add family storage policies, ownership transfer, and shared retention controls.

## Release gates for every tranche

1. Ownership and authorization tests cover every read and write path.
2. Forward and downgrade migration SQL is inspected.
3. Backend tests, frontend production build, and mobile type checks pass.
4. Data-loss paths have an explicit rollback or verified recovery procedure.
5. Secrets and generated media never enter source control or build contexts.
6. Documentation reflects the behavior actually delivered, not planned work.
