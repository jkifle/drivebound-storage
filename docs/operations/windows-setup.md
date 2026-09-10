# Guided Windows setup

Drivebound's Windows pilot is designed for someone who does not use a terminal
or generate encryption keys. It runs on a PC that stays awake with its drives
connected. Hardware and cellular acceptance still need the
[stationary-PC pilot checklist](stationary-pc-pilot.md).

## First installation

1. Install Docker Desktop, open it, and finish its Windows/virtualization prerequisites.
2. Double-click **Drivebound Setup** in the downloaded project folder.
3. Enter the email address of the person who owns this PC's photo collection.
   Only that verified account can connect the host photo folder. Other accounts
   can use their own uploads; registration does not grant access to the PC's files.
4. Select the existing-photo folder, where new uploads should go, and (if
   available) a second physical drive for protection copies and database backups.
   Select photo subfolders, not an entire drive. Network shares and junctions
   are outside this Windows pilot.
   Use drives supporting Windows file permissions (such as NTFS); do not reformat
   a drive containing photos just to run the pilot.
5. Choose whether Drivebound should start after you sign in to Windows.
6. Wait for the browser to open. Create and verify the approved account, name
   the collection, and confirm the selected folder. No container path is required.
   An import can continue in the background; a failed import offers a retry.
7. Open **Storage**, choose **Back up and verify now**, and wait for both the
   database and configuration archive results. Do this before the recovery drill.

Setup generates the database password, media key, signing key, MFA key and
monitoring token automatically. It restricts configuration files to the Windows
account running setup and SYSTEM. Keep using that Windows account to operate
the installation. Do not share `.env`, `.drivebound`, or disk identity markers.

The private `.drivebound/installation.json` remembers folder choices, approved
owner, volume identities and the Docker project name. `docker-compose.user.yml`
contains read-only import mounts and refuses to create missing host paths.
Storage identity markers are kept outside the imported photo folder.

## Normal operation and recovery

- **Drivebound Start** starts the saved installation and checks the frontend,
  database, API, Redis, background worker, scheduler and approved storage.
- **Drivebound Stop** stops without deleting photos/accounts and records your
  intent, so the sign-in startup task stays stopped until you select Start.
- **Drivebound Status** writes a redacted `.drivebound/status-report.json` and
  explains whether Docker, storage and the application are ready.
- Running **Drivebound Setup** again preserves keys, account/database identity,
  remote settings, owner and selected folders. It is not a drive-migration or
  account-transfer tool. Missing keys or wrong disks require recovery of the
  original configuration, never a new encryption key.
- If first setup is interrupted, retry Setup with the same drives connected.
  Its private recovery draft reuses the approved keys and folder identities.
  A later interrupted configuration change rolls back before retrying.

Automatic startup is a **Windows sign-in** task running as the installing user,
with a five-minute execution limit. Enable Docker Desktop's start-on-sign-in
option too. Keep the PC awake. This does not establish cold-boot operation before
anyone signs in, and five-minute recovery must be measured on the pilot PC.
[Docker startup settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/).

Existing pre-manifest installations can be adopted when their original `.env`
and generated mount configuration are intact. An ambiguous custom overlay,
missing credential, changed owner or relocation fails with recovery guidance.
File-backed-secret production deployments must retain their existing deployment
workflow; this wizard will not rewrite them.

### Docker closes with an Inference manager error

If Docker reports `initializing Inference manager` and cannot access
`Docker/run/dockerInference`, the container engine has not started. This is a
Docker Desktop runtime problem, not an incorrect Drivebound password, media key,
or Compose port.

1. Choose **Quit** in Docker's error dialog. Do not choose **Reset to factory
   defaults**, **Clean up data**, or uninstall/delete Docker's data folders.
2. Update Docker Desktop using the Windows installer in the
   [official release notes](https://docs.docker.com/desktop/release-notes/).
   As of September 10, 2026, the current release is **4.90.0**. Releases 4.89.0
   and 4.90.0 list a Windows startup fix for a stuck socket after an ungraceful
   shutdown. The pilot encountered this error on 4.86.0; matching the symptoms
   does not establish that an update has repaired this particular PC yet.
3. Restart Windows if requested by the installer, then open Docker Desktop and
   wait for its engine to run. Double-click **Drivebound Start** afterward.
4. If the error continues, retain the error and use Docker's
   [troubleshooting guidance](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/).
   Diagnostic uploads send information to Docker; review and approve that step
   yourself. Ask for a targeted repair before changing runtime directories.

Keep the existing Drivebound configuration and recovery set. Generating new
keys, unregistering Docker's WSL distributions, or resetting Docker would not
be a safe response to this startup error.

## Private remote access

1. Install Tailscale on the PC and phone/laptop. Sign in to the intended private
   Tailscale network. Enable MagicDNS and HTTPS certificates in that network.
2. On the PC, double-click **Drivebound Remote Access**.
3. Enter your email provider's SMTP server, STARTTLS port (usually 587), sender,
   username and app password. The wizard sends one test email to the approved
   owner before applying settings. Check that it arrives. Real email delivery is
   required for verification and password recovery; a development link is not a
   remote-access substitute.
4. Wait while Drivebound rebuilds its browser configuration, starts with remote
   security checks, and enables private HTTPS routes. Existing unrelated
   Tailscale services are not overwritten. Conflicting routes require review.
5. Open the displayed **https://PC.NETWORK.ts.net** address from a phone with
   Wi-Fi turned off and Tailscale connected. Sign in, browse, download and upload.
   In the native mobile app, use **https://PC.NETWORK.ts.net:8443** as the server.

The browser uses HTTPS 443; the API uses HTTPS 8443 on the same private hostname.
Host-facing application ports remain loopback-only 3000 and 8000. No router port
forwarding or public Tailscale Funnel is configured. Device access follows your
Tailscale access policy; review which people/devices are allowed.
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).

The wizard enables `REMOTE_ACCESS_ENABLED`, SMTP/STARTTLS, secure host-prefixed
cookies, and consistent browser/API/passkey/CORS origins. It preserves encryption
and account secrets and does not trust arbitrary proxy headers. It does not
convert this pilot into a production-certified deployment.

Changing from localhost to the private hostname changes the passkey relying
party. Ensure password or Google sign-in works first, then enroll a passkey for
the new address. If using Google, update its authorized callback to
`https://PC.NETWORK.ts.net:8443/api/v1/auth/google/callback` in Google Cloud.
A failed remote setup retains or restores safe configuration and explains the
next step. If route cleanup fails it keeps HTTPS guards enabled; do not revert
manually to insecure local settings while those routes remain active.

## Drive safety and the recovery set

Never overlap imported originals with managed originals, staging, previews,
replicas or backups. Different folders/partitions on one disk are not independent
protection. Setup reports **same_disk**, **independent_disk**, or **unverified**;
the protection disk must differ from both the imported-originals disk and the
managed-upload disk to qualify as independent. Only verified distinct physical
disks satisfy the drive-failure pilot gate.

A missing or wrong drive blocks affected storage operations. Reconnect the
original drive and retry Start or the failed operation. Catalog records and
cleanup evidence must be retained while storage is unavailable. Do not remove
identity markers or recreate empty folders to silence a warning.

Keep an access-controlled recovery copy of `.env`, `.drivebound`,
`docker-compose.user.yml`, the configured media/protection/backup folders, and the
same-volume `.drivebound-identities` directories. The account-deletion suppression
ledger is under the selected protection folder's
`backups/account-deletion-suppressions`; retain it independently of historical
DB dumps. These files include sensitive encryption material: do not put them
in shared diagnostics or source control.

Operational configuration archives intentionally omit secrets. A verified DB
archive means readable structure plus checksum, not proven disaster recovery.
Use the separate [recovery drill](recovery-drill.md) before claiming a full restore.

## Administrator and developer checks

Ordinary users use the double-click launchers. Administrators can configure a
fresh isolated installation with `tools/setup-drivebound.ps1 -Action Setup
-NonInteractive -NoStart -OwnerEmail owner@example.test -ExistingPhotosPath ...
-ManagedDataPath ... -ProtectionDataPath ...`. `-NoStart` only prepares local
configuration; it is not a live Docker/database acceptance check. Starting a fresh
installation refuses an already-existing Docker database without original credentials.

Run `tools/test-setup.ps1` for temporary-folder setup/recovery contracts and
`tools/test-remote-access.ps1` for remote-origin/security contracts without changing
Tailscale or sending email. `tools/validate.ps1` runs these with the backend,
frontend and mobile checks. Physical volumes, reboots and cellular connectivity
remain operator-run evidence.
