# Guided Windows setup

Drivebound's Windows setup is designed for a person who does not work with
terminals or encryption keys.

## First installation

1. Install and open Docker Desktop once.
2. Double-click **Drivebound Setup** in the Drivebound folder.
3. Choose the folder containing existing photos, if there is one. Drivebound
   mounts this folder read-only.
4. Choose where new uploads should be stored.
5. If available, choose a folder on a second physical drive for protection
   copies and database backups.
6. Wait for the browser to open, create an account, and complete onboarding.

The setup creates all required random values automatically, including the
database password, media-encryption master key, login signing key, MFA
encryption key, and monitoring token. These are written to the ignored local
`.env` file and reused on every later run. Setup never prints them or asks the
person installing Drivebound to copy them.

The selected host folders are recorded in the ignored
`docker-compose.user.yml` file. The existing-photo folder is available to the
web onboarding flow as `/data/imports`.

## Normal operation

- Double-click **Drivebound Start** to start the existing installation.
- Double-click **Drivebound Stop** to stop it without deleting data.
- Run **Drivebound Setup** again only to repair placeholder configuration or
  choose different storage folders. When setup finds an existing `.env`, it
  asks whether the installation has ever stored an account or managed photo.
  Answering **Yes** preserves every existing secure value. Answer **No** only
  for a brand-new, unused copy of the example configuration.

Docker services use `restart: unless-stopped`, but Docker Desktop itself must
be configured to start when the Windows user signs in. The PC must also remain
awake for remote devices to reach it.

## Storage safety

Existing-photo imports must not overlap Drivebound's managed upload or
protection folders. The setup blocks this unsafe arrangement. Protection copies
on the same physical drive as originals exercise the workflow but do not protect
against physical drive failure; select a second drive when possible.

Back up the selected backup folder and the `.env` file together to an
access-controlled location. The media-encryption master key in `.env` is needed
to decrypt managed originals after a disaster. Do not rerun setup from a fresh
template over an existing installation without retaining that file.

## Administrator automation

The same setup engine supports unattended configuration for testing or managed
installations:

```powershell
powershell.exe -NoProfile -File tools\setup-drivebound.ps1 `
  -Action Setup -NonInteractive -NoStart `
  -ExistingPhotosPath "E:\Photos" `
  -ManagedDataPath "E:\Drivebound" `
  -ProtectionDataPath "F:\Drivebound"
```

This interface is for administrators. Ordinary users should use the
double-click launchers.
