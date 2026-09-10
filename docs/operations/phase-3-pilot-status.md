# Milestone 1 — Phase 3 integration and release gate

Run date: **2026-09-10**. Tested working-tree changes based on `cf0ed07`;
not a committed or published release.

**Gate: not passed.** Local regression checks can proceed, but live-stack
acceptance is blocked by Docker Desktop failing before its Linux engine starts.
No physical, remote, or complete disaster-recovery check is credited as passed.

## Host blocker and safety boundary

- Docker is a per-user Windows installation. Its logs identify Desktop
  **4.86.0**, and the CLI reports Compose **v5.3.1**.
- The host reports Windows build **26200.9168**, display version **25H2**.
- Starting Desktop resulted in `initializing Inference manager` and an
  inaccessible `dockerInference` runtime socket. The Linux-engine named pipe
  remained absent. Existing containers, images and volumes could not be
  inventoried through the engine; no claim is made that they are empty.
- The official [4.90.0 release notes](https://docs.docker.com/desktop/release-notes/#4900)
  list a fix for Windows startup failures caused by stuck socket files. The
  operator was advised to quit and update, not reset Docker. The updated engine
  has not been verified on this host.
- No `.env`, encryption key, real media, Docker volume, runtime socket directory,
  Tailscale route, account or SMTP setting was changed. Docker Desktop was
  launched for testing; no Drivebound test stack was started.

See the [Docker startup guidance](windows-setup.md#docker-closes-with-an-inference-manager-error).
Do not bypass this blocker with a fresh database, regenerated keys, factory
reset or unregistered WSL distribution.

## Local regression evidence

Validation and integration-review fixes are in progress. Record final commands
and counts here after their checks complete; the earlier Phase 2 counts are not
substitutes for a new run.

## Acceptance disposition

| AC | Current Phase 3 evidence | Required before passing |
| --- | --- | --- |
| 1 — Double-click setup | Setup contracts and native-command error review; Docker startup failed on the host | Updated Docker starts, then a real interactive setup succeeds without commands or manual secrets |
| 2 — Preserve installation | Temporary-folder rerun/crash-recovery contracts | Existing accounts and encrypted media remain usable after a real rerun |
| 3 — Independent storage | Review identified that protection must be distinct from both import and managed disks | Real volume/physical-disk mapping, permissions and in-container read-only import mount |
| 4 — Missing/wrong disk | Marker and non-creating-mount contracts | Live absent-drive and wrong-drive-at-same-letter checks |
| 5 — Readiness | Dependency and heartbeat contract tests | Real worker/scheduler startup, dependency interruption and measured readiness recovery |
| 6 — Owner onboarding | Approved-owner and unauthorized-account API contracts | Browser registration, verification, connect/import/retry, and second-account denial |
| 7 — Imports/metadata | Existing import and metadata unit coverage | Disposable photo/video hashes, source timestamps, embedded metadata, playback and duplicate/rescan comparison |
| 8 — Remote access | Origin/cookie/SMTP requirement and route-conflict contracts | Actual SMTP arrival and Tailscale HTTPS cellular sign-in, upload/download and access-policy denial |
| 9 — Startup/Stop | Saved-intent and setup contracts | Reboot/sign-in recovery time and persistent explicit Stop; no-login startup remains unproven |
| 10 — Disconnect/reconnect | Storage guards and recovery-failure review | Live interruption of each role, retry after reconnection and preserved catalog evidence |
| 11 — Protection restore | Checksum/restore contracts and failed-publication review | Photo and video restoration from a distinct disk, corrupt-copy rejection, unchanged import source |
| 12 — Operational backups | Backup-lock and archive-evidence review | First actual PostgreSQL/config archive and corruption/missing-archive detection; full isolated recovery remains the separate Milestone 2 gate |

## Resume plan

1. Confirm the updated Docker engine responds, then inspect existing project
   names and port bindings without changing them.
2. Use a uniquely named disposable Compose project, explicit test environment
   and paths, and non-conflicting loopback ports. Never implicitly load the real
   installation's `.env` or reuse its database volume.
3. Run the fixture-based owner/import/upload/metadata/backup journey. Revalidate
   copied bytes and archive evidence after controlled test failures.
4. Have the operator complete the [stationary-PC checklist](stationary-pc-pilot.md)
   for SMTP, cellular access, reboot and physical-disk recovery. Retain redacted
   evidence for every AC before marking the milestone release-ready.
