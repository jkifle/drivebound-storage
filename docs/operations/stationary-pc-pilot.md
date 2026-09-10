# Stationary-PC pilot acceptance

This pilot tests one Windows PC that stays powered on with its photo drive
connected, and a phone or laptop accessing it remotely. Passing automated tests
does not complete the pilot: record the real-host checks below for the exact
release being installed. A check that has not been performed is **not tested**,
not passed.

See [Phase 3 integration status](phase-3-pilot-status.md) for the current gate,
and [Phase 2 implementation evidence](phase-2-pilot-status.md) for the preceding
implementation results. Neither report replaces the physical run below.

## Before starting

Use a disposable photo collection and a separate test account. Keep the real
collection outside the selected test folders. Download or clone the same
release on the stationary PC, install and open Docker Desktop, and follow the
[guided Windows setup](windows-setup.md). Use **Drivebound Setup**, **Drivebound
Start**, and **Drivebound Stop** for ordinary operation.

The PC needs Windows sign-in for Docker Desktop's configured startup. Record
that dependency; this pilot does not demonstrate operation before Windows
sign-in. A protection folder on the same physical disk exercises copying but
does not establish protection against disk failure. Have a second disk for
the protection and restore checks.

Have the pilot operator prepare a reference copy of these files outside the
import and managed-storage folders:

| Sample | Expected checks |
| --- | --- |
| JPEG with a known capture date, camera make/model, and GPS coordinates | Capture date, camera metadata, location, original bytes |
| Image with no GPS or capture date | No invented location or capture date; documented fallback date |
| Photo with a non-ASCII filename, spaces, and nested folder | Filename, folder association, original bytes |
| Short supported video with sound and container metadata | Playback, sound, duration, available date/location, original bytes |
| Two differently named files with identical bytes | Record intended duplicate handling; repeated scans do not multiply records |

Record expected byte lengths, SHA-256 checksums, source last-modified times,
and available embedded metadata before importing. These values belong to the
operator's test record; an ordinary user should not need to produce them to
install or use Drivebound. Browser downloads can receive a new filesystem
creation/modified time: check the recorded asset metadata and original bytes
separately from the download folder's timestamps.

## User journey

1. Double-click **Drivebound Setup** from the downloaded release. Approve the
   email address that will own the PC's photo collection. Select the disposable
   existing-photo folder, managed-storage folder, and protection folder.
   Record whether each step is understandable without terminal
   commands, configuration editing, or copying an encryption key.
2. Wait for setup to report readiness and open the application. Create the test
   account using the approved owner email address, complete the available
   verification flow, confirm the selected
   photo folder, and finish onboarding. Record any unexpected server paths,
   hidden failure, or unusable button.
3. Watch the import finish. Compare the resulting library with the fixture
   record. Open each image and video, inspect available metadata, and download
   originals. Rescan once; existing records must not multiply, and the source
   collection's bytes and last-modified times must remain unchanged.
4. Upload a fresh disposable photo through the browser. Wait for processing
   and checksum-verified protection to finish. Download it again and compare
   it with the reference copy.
5. Close the browser and run **Drivebound Start**, then sign in again. Run Setup
   again using its existing-installation journey. Confirm that the account,
   selected folders, remote configuration if enabled, and previously uploaded
   encrypted media remain usable.

An import request being accepted is not proof that a scan finished. Any queue,
worker, or scan failure must remain visible with a retry path. “Ready” must not
hide a failed or disconnected required service.

## Remote access

Double-click **Drivebound Remote Access** and use the same private Tailscale
network on the stationary PC and external device. Third-party installation,
sign-in, and authorization steps still belong to the user. Use the HTTPS
address shown by the setup journey; the external device must never be asked
to use `localhost` or a container address.

With Wi-Fi disabled on the phone, or from a genuinely different network:

- Open the landing page, sign in, browse imported photos, play the video, and
  download an original. Verify its bytes against the reference copy.
- Upload a new disposable photo remotely. Confirm it appears locally and
  remotely with the available original metadata and a verified protection copy.
- Sign out and sign back in. Verify that the stable HTTPS address still works
  after the browser and remote-access application restart.
- Check from an unauthorized device that no private library or media is
  returned. An authenticated second account must not be able to claim or scan
  the host owner's selected import folder.

Record whether verification and password recovery use real delivered email or
an explicitly local test mechanism. Do not credit SMTP delivery based on a
development link. Remote access must use the configured security mode; changing
an environment flag to bypass a failed security check does not pass the pilot.

## Reboot and drive interruption

Use these checks only on the disposable installation. Perform one interruption
at a time and allow it to settle before moving to the next.

| Action | Pass condition |
| --- | --- |
| Reboot a previously running PC and sign in to Windows | Docker, the application, and private remote access recover without Drivebound commands. Record elapsed time; the pilot target is five minutes after Windows sign-in. |
| Stop Drivebound, then reboot/sign in | The deliberately stopped installation stays stopped until **Drivebound Start** is selected. |
| Disconnect the existing-photo disk | Availability is reported accurately; source catalog records and healthy copies remain; no external original is deleted. |
| Reconnect the same existing-photo disk | The selected library returns through the supported retry/rescan journey without duplicate import or new configuration. |
| Disconnect managed storage, then attempt a disposable upload | The upload pauses or fails visibly and can be retried. The application does not accept a replacement empty folder as the original disk or silently write to another disk. |
| Disconnect the protection disk | The application reports unavailable/degraded protection, keeps healthy originals and existing replica records, and does not call unverified copies protected. A service that requires unavailable backup/deletion-ledger storage may remain unavailable with an actionable message. |
| Reconnect the same protection disk | Verification/repair can resume without key regeneration, loss of catalog evidence, or rewriting unrelated data. |

Before unplugging, stop active test transfers and use the operating system's
eject workflow where available. If the operating system refuses ejection, the
operator should use a controlled disposable-environment fault simulation;
force-unplugging a disk containing real data is not an acceptance requirement.

An operator should additionally test absent/wrong storage identity, missing
configuration, conflicting ports, stopped workers, unavailable queues, and an
interrupted setup publication against isolated copies. Missing deployment keys
or legacy database/storage evidence must lead to recovery guidance rather than
automatic generation of replacement encryption state. Record these separately
from the ordinary user journey.

## Protection and restoration

Wait until a photo and video each have a checksum-verified copy on a verified
different physical disk. Use Drivebound's supported restore workflow against
disposable originals; retain reference copies outside the test paths. Download
the restored originals and compare byte length and checksum, then inspect
capture date, available GPS/camera/container metadata, and playback.

Record what was actually restored: original download, Trash restoration, and
repair from a protection copy prove different behaviors. A Trash-only restore
does not prove recovery from disk loss. Restoring one file does not prove that
accounts, albums, ownership, or the database can be recovered after losing the
PC. That remains the isolated full-restore gate in the
[recovery drill runbook](recovery-drill.md), including the retained secrets and
account-deletion suppression ledger.

## Evidence record

Keep one redacted record for the run. Do not include `.env`, passwords,
encryption keys, authentication tokens, private photo contents, or full personal
paths in shared diagnostics.

| Field | Value to record |
| --- | --- |
| Release | Commit or release identifier; date tested |
| Stationary PC | Windows and Docker Desktop versions; CPU/memory; Windows sign-in dependency |
| Storage | Logical roles and whether disks are physically distinct; avoid serial numbers in shared reports |
| Remote device | Device/OS/browser and whether cellular or another network was used |
| Prerequisites and setup | Passed / failed / not tested, evidence and open issue |
| Existing-installation rerun | Passed / failed / not tested, evidence and open issue |
| Account, imports, metadata, uploads | Passed / failed / not tested, fixture results and open issue |
| Remote browsing/upload and ownership | Passed / failed / not tested, evidence and open issue |
| Reboot | Passed / failed / not tested, measured recovery time and open issue |
| Drive interruptions | Separate result for imports, managed storage, protection, and reconnection |
| Protection-copy restoration | Passed / failed / not tested, sample checksum/metadata result and open issue |
| Full database/media recovery | Not tested unless the separate isolated restore runbook was completed |

The stationary-PC pilot passes only when every required user-journey, remote,
reboot, drive-interruption, and protection-copy restore check has retained
evidence and no unresolved data-loss or ownership failure. Record automated
test results as supporting evidence, never as substitutes for physical-host
checks.
