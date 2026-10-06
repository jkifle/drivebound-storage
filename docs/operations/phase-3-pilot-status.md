# Milestone 1 — Phase 3 integration and release gate

Latest checkpoint: **2026-10-05**. Initial checkpoint: **2026-09-10**.
The checkout advanced from `cf0ed07` to `2888424` during this phase. Validation
covers `2888424` plus the local regression fixes; this report is not a published
release or approval to deploy.

**Gate: not passed.** Local regression checks can proceed, but live-stack
acceptance is blocked by Docker Desktop failing before its Linux engine starts.
No physical, remote, or complete disaster-recovery check is credited as passed.

## October 5 setup diagnostics checkpoint

Setup and remote-access failures now retain a private, bounded diagnostic log
with the active phase, exception type, script location, and sanitized native
failure output. Double-click launchers preserve the exit code and hold the
console open on interactive failures, including PowerShell startup/parser
failures. Unattended runs do not pause. See the
[error-reporting instructions](windows-setup.md#setup-fails-or-its-window-closes).

A real read-only probe exposed a Docker executable-discovery failure: multiple
command matches could be passed as an array where a single executable path was
required. Setup now selects one `docker.exe`, supports the per-user install
locations, and explicitly checks for a Linux-container engine. The corrected
probe reached Docker and reported its missing Linux-engine pipe. This is not
evidence that Docker Desktop startup or the live application has been repaired.

The full local validation gate passed (272 backend tests, 1 skip; frontend build,
lint with 9 existing warnings, migration SQL, mobile typecheck, and contracts).
Final targeted reruns passed: 47 setup assertions, 32 remote assertions,
56 diagnostic assertions, 18 setup-failure assertions, and 50 launcher assertions.
Failure tests use disposable configuration and mocked services; one real
temporary native process verifies timeout output retention. No actual setup,
SMTP, Tailscale mutation, or Docker service startup was performed on the host.

## October 3 checkpoint

- The complete `tools/validate.ps1` gate passed: **272 backend tests passed,
  1 platform-specific skip, 2 existing warnings**; **47 setup assertions**;
  **28 remote-access assertions**; isolated runner contracts; migration SQL;
  frontend lint (**0 errors, 9 existing warnings**) and production build;
  mobile typecheck; and patch whitespace checks.
- The isolated runner contract suite mocks Docker and preparation. The Python
  acceptance-client suite has **16 tests**, including a mocked 11-step API
  journey and linked-ancestor rejection. Neither suite proves a running stack.
- Real `-PrepareOnly` execution succeeded using the guided installer, generated
  disposable secrets, and generated JPEG/PNG fixtures in a private run folder.
  It exposed and fixed a fresh-setup bug: an explicit Compose project name in
  the template was ignored. Rerun contracts confirm existing project identity
  is preserved when a template changes.
- Compose **v5.3.1** parsed that real prepared configuration successfully. The
  runner verified six expected services, private project/volume/network names,
  local database and queue addresses, guarded test-only mounts, and two distinct
  high loopback ports. This was `compose config`, not container execution.
- The validated preparation is run `4d602741e4de44b9985167deb0ee083f`, under
  `.drivebound/pilot-runs/`. The prior preparation that exposed the naming bug
  was retained for diagnosis; it did not start services.
- Docker's Linux engine remained unavailable on a fresh read-only check. No
  runtime acceptance report was produced and no test containers were started.
  The real installation's `.env`, keys, media, accounts, and remote settings
  were not changed. Generated test configuration is private and must not be
  shared as evidence.

Additional integration fixes now cover Unicode/special-character download
filenames for plaintext and encrypted originals, secret-file exclusions from
the frontend Docker build context, and isolated test-runner safety. Docker calls
are pinned to a validated local named pipe; remote endpoints and linked run
ancestors are rejected. Existing reports prevent a second live run. Timeout
messages distinguish an existing report from a process that produced no report.
The developer validator uses fresh short temporary paths to avoid stale pytest
permissions and Windows path-length failures.

### Run the isolated developer acceptance test

These commands are for developer validation, not the nontechnical installation
journey. Run from the repository root with the project's Python environment
installed. Preparation does not require Docker to be running:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-pilot-stack.ps1 -PrepareOnly
```

After Docker Desktop's Linux engine is healthy, create and test a new run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/test-pilot-stack.ps1
```

The runner creates a unique disposable project and chooses loopback ports;
it does not load the repository's real `.env`. It checks owner verification and
sign-in, second-account denial, import/deduplication/rescan, resumable encrypted
upload, original download checksums, capture/date/GPS metadata, protection-copy
restore, PostgreSQL/configuration archive verification, and unchanged source
bytes/timestamps. It checks frontend HTTP availability, not browser interaction.

To use an already prepared, unreported run, pass `-RunDirectory` with the exact
private folder printed by preparation. A completed or failed report is retained;
use a new run for another acceptance attempt. By default, the runner removes
only its containers/network after an attempted start; it retains named volumes,
fixtures, and configuration. `-KeepRunning` leaves the test stack running for
inspection. It does not start Docker Desktop or alter Docker settings.

Only `report.json` is designed as redacted acceptance evidence. If startup,
readiness, or the acceptance process fails before a report can be written, record
that failure explicitly; a missing report is not a pass. Do not share private
`.env`, installation/recovery files, or raw container logs without review.

For the actual PC and external device, follow the
[laptop and phone test guide](remote-device-test-guide.md). Real email delivery,
Tailscale/cellular access, video playback, reboot, physical-disk behavior, and
full isolated disaster recovery remain separate, unpassed gates. The acceptance
disposition below is unchanged by preparation or mock-based checks.

## September 10 host blocker and safety boundary

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

## September 10 local regression evidence

- Combined developer gate passed: **220 backend tests passed, 1 platform-specific
  skip, 2 existing warnings**, with the setup, frontend, mobile, migration and
  whitespace checks below also passing.
- Windows PowerShell 5.1 setup contracts: **44 assertions passed**. These include
  real native-process argument/stderr handling and a mocked single-installation
  Docker startup path. Volume identities remain mocked; no Desktop or scheduled
  task is launched by the contract suite.
- Remote contracts: **28 assertions passed** without services. The optional
  `tools/test-remote-access.ps1 -ComposeRoundTrip -DockerExecutable <docker.exe>`
  run passed **39 assertions total** against Compose v5.3.1: ten disposable
  credential strings survive the real parser, including trailing backslashes,
  quotes, dollar expressions, repeated dollars, whitespace and empty values.
  This uses `compose config`, not a running container or real email provider.
- Backend recovery regressions: **13 passed** in
  `backend/tests/test_pilot_recovery_regressions.py`. These manipulate only
  temporary bytes and marker fixtures; database sessions, broker publishing
  and PostgreSQL archive utilities are mocked. Restored fixture bytes are
  copied and checksum-compared through the real restore implementation.
- Frontend production build passed; lint passed with **0 errors, 9 existing
  warnings**. No new browser/account integration result is claimed.
- Mobile typecheck passed; no signed Android/iOS release was produced.
- Alembic offline upgrade SQL passed; the single migration head remains **0017**.

The combined developer gate is `powershell.exe -NoProfile -ExecutionPolicy
Bypass -File tools/validate.ps1`. It does not include Docker, SMTP, physical
hardware or cellular acceptance. The actual Compose parser check above is a
separate opt-in run and never reads the real installation's `.env`.

## Integration-review fixes

- Native stderr no longer aborts the Windows readiness check before Docker can
  be started. CLI arguments are quoted without shell expansion; Tailscale
  cleanup continues across failed routes and preserves secure settings when
  cleanup cannot be confirmed. A single discovered Desktop executable remains
  a complete path instead of being indexed as one character. The missing
  **Drivebound Status** launcher is supplied.
- Protection classification includes both imported originals and managed
  uploads; sharing either source disk is not independent protection.
- Remote SMTP literals use Compose-compatible escaping. Parser tests account
  for Compose's display-time dollar escaping rather than comparing generated
  strings alone.
- Recent database backups are revalidated before reuse. Previously verified
  archives are eligible for reverification, and recovery readiness checks current
  checksum/size evidence instead of trusting old verification timestamps.
- Interrupted restoration records a retryable failure. Automatic-recovery
  events are no longer permanent queue locks: failures can retry, and an expired
  15-minute queue lease can recover from a lost worker/broker callback.
- Destination-write failures retain resumable staging bytes for encrypted and
  unencrypted publication. An existing encrypted object must authenticate before
  its staged source is discarded.

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
