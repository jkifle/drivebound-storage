# Milestone 1 — Phase 2 implementation evidence

Validated locally on 2026-09-09 against the working-tree changes based on
`cf0ed07`. No deployment, commit, account change, real drive interruption,
Tailscale configuration, or SMTP send was performed during this implementation.

## Automated checks

- Backend: **207 passed, 1 platform-specific skip**, 2 existing warnings.
- Frontend: production build passed; lint **0 errors, 9 existing warnings**.
- Mobile: typecheck passed. No signed mobile release built in this phase.
- Alembic: linear head remains **0017**; offline upgrade SQL passed. Host approval
  and installation state use a private local manifest, not a new database table.
- Windows setup: temporary-folder contracts verify generated secrets/private
  ACLs, rerun preservation without path arguments, transaction rollback, exclusive
  setup lock, interrupted first-run resume, missing key/wrong marker refusal,
  orphaned configuration/managed-data refusal, same-disk labeling and read-only
  non-creating bind mounts. Physical volume identity is mocked in these tests.
- Remote setup: **25 assertions** for private hostname/origin/cookie settings,
  SMTP/STARTTLS requirements, secret preservation, literal password escaping,
  conflicting routes and public Funnel refusal. No remote services changed.
- Local landing route compiled and returned HTTP 200. Full browser/account
  interaction with a live backend was not exercised.

Run the complete developer gate with `tools/validate.ps1`. This does not include
Docker or Android unless their optional switches are supplied. Run
`tools/test-setup.ps1` and `tools/test-remote-access.ps1` independently for the
Windows contracts.

## Acceptance mapping and remaining evidence

| AC | Implementation available | Physical/integration evidence still required |
| --- | --- | --- |
| 1 — Nontechnical setup | Double-click folder/owner setup; automatic keys; Docker, virtualization and port-conflict guidance | Fresh supported Windows install and interactive folder/dialog usability |
| 2 — Preservation and recovery | Stable project/folders/keys; private journal and first-run draft; orphaned data/key refusal | Adopt a real previous installation, sign in and decrypt its existing media |
| 3 — Storage separation | Overlap/junction rejection; read-only imports; Windows volume/physical-disk comparison; explicit unverified label | Two real partitions vs two physical disks; supported filesystem permissions |
| 4 — Missing/wrong disk | Host identity check before start; non-creating binds; runtime identity checks before storage I/O | Missing disk and another disk assigned the same drive letter during operation |
| 5 — Readiness and retry | Separate readiness endpoint for DB/Redis/storage/worker/scheduler; frontend probe; redacted Status and Start retry | Stop each Compose dependency and measure the reported state/recovery |
| 6 — Owner onboarding | Verified, locally approved owner; friendly selected folder; idempotent connect; scan errors and retry | Complete account verification/import flow, including a second unauthorized account |
| 7 — Imports and metadata | Source reads remain read-only; per-account deduplication; same-batch duplicate protection; queue-retry handling | Disposable photo/video fixture hashes, GPS/date/camera/playback and rescan comparisons |
| 8 — Secure remote access | Private Serve routes 443/8443; consistent origins; secure cookies; mandatory SMTP test; safe failure handling | Real SMTP arrival, Tailscale policy/HTTPS, cellular sign-in/browse/download/upload and unauthorized device denial |
| 9 — Startup and Stop | Optional single-instance sign-in task; bounded startup; persistent explicit Stop | Actual reboot/sign-in within five minutes; cold boot before sign-in remains unsupported/unproven |
| 10 — Disconnection recovery | Storage-aware scans/uploads/protection/restores/cleanup; preserve catalog/deletion evidence on unavailability | Separate imports/managed/protection disconnect/reconnect tests and healthy record preservation |
| 11 — Verified protection restore | Existing checksum-gated managed restore plus storage identity guards; imported originals remain untouched | Representative photo and video restored from a distinct physical disk; corrupt replica rejected |
| 12 — Operational recovery | Owner-only immediate backup action; duplicate-delivery/run lock; verified archive list; documented secret/ledger recovery set | First live DB/config archive verification; full isolated database/media recovery is a separate Milestone 2 gate |

## Scope and cautions

The [Windows guide](windows-setup.md) is the ordinary-user journey. The
[stationary-PC pilot checklist](stationary-pc-pilot.md) records real evidence.
Guided setup preserves an installation; it is not a drive relocation, ownership
transfer, or production-secret migration tool. This pilot requires local paths
on filesystems supporting Windows permissions; network shares, junction paths,
and non-permission filesystems need a separately designed support path.

Automatic startup requires the installing Windows account to sign in, and the
PC must stay awake. A readable/checksummed database archive or successful file
restore is not proof of complete disaster recovery. Preserve the separately
documented encryption/configuration files and suppression ledger. Do not mark
Milestone 1 passed until the physical acceptance checklist has retained evidence.
