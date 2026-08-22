# Drivebound

Drivebound is an expandable personal cloud backed by drives you own. It provides account-isolated photo libraries, resumable checksum-first uploads, background media processing, storage expansion, verified protection copies, private sharing, albums, search, and map discovery.

## Start

On Windows, double-click **Drivebound Setup** in the project folder. The guided
setup lets you choose an existing photo folder, new-upload storage, and an
optional second protection drive. It creates the database password, encryption
key, authentication secrets, storage folders, and private Docker configuration
automatically. It then starts Drivebound and opens the account-creation page.

Future use requires only **Drivebound Start** and **Drivebound Stop**. Stopping
the application never deletes photos or accounts. Running setup again preserves
the existing encryption key and account data.

See the [guided Windows setup](docs/operations/windows-setup.md) for the complete
storage and recovery explanation.

For command-line or non-Windows development, copy `.env.example` to `.env`,
replace every placeholder secret, and run `docker compose up --build -d`.

The backend applies Alembic migrations through `0017` before starting. Confirm the current migration with:

```text
docker compose exec backend alembic current
```

If an older database reports exactly `0014`, follow the
[duplicate-revision reconciliation note](docs/operations/migration-reconciliation.md)
before upgrading; that revision briefly existed in an ambiguous development
branch and must be identified from its schema markers.

Service health is available at <http://localhost:8000/api/v1/health>. Interactive API documentation is available at <http://localhost:8000/docs>.

## Accounts and ownership

Registration, login, logout, current-user lookup, and onboarding are implemented. Browser sessions use an HTTP-only, same-site cookie. Private routes derive ownership from the authenticated account rather than trusting a browser-supplied user ID.

Assets, resumable uploads, libraries, devices, albums, storage status, protection actions, and share creation are account-scoped. Duplicate detection is scoped per account so one user cannot discover another user's files through checksum responses.

Email verification, password recovery and changes, rotating revocable refresh sessions, active-device controls, security history, Redis-backed login throttling, authenticator-app MFA with one-use recovery codes, account export/deletion, and a trusted-host recovery command are implemented. Account deletion immediately disables live access and destroys the live account media key, then a leased background job removes catalog-owned managed bytes. It does not instantly erase historical database/off-site archives; operators must preserve and replay the independent suppression ledger until every older recovery copy has been disposed of. See the [account-deletion runbook](docs/operations/account-deletion.md). Before broad internet exposure, configure SMTP and a trusted HTTPS ingress, replace both secrets, and set `AUTH_COOKIE_SECURE=true`.

Local development uses `EMAIL_DELIVERY_MODE=console`; verification and reset screens expose their development-only links. Production must use `EMAIL_DELIVERY_MODE=smtp` and configure `SMTP_HOST`, `SMTP_FROM`, and any required credentials. Administrative recovery is deliberately not exposed over HTTP; run `.venv\Scripts\python.exe tools\account_recovery.py --help` from the project folder on a trusted host. It securely prompts for a replacement password (or reads a restricted password file) and revokes sessions, mobile devices, paired nodes, passkeys, MFA/recovery codes, pending account tokens, WebAuthn challenges, and linked external identities.

### Google sign-in

Create a Google OAuth client with the **Web application** type, then add this exact authorized redirect URI for local development:

```text
http://localhost:8000/api/v1/auth/google/callback
```

Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`, keep the secret out of source control, and rebuild the backend. For production, also set `API_URL`, `APP_URL`, and `GOOGLE_REDIRECT_URI` to their HTTPS public addresses. Drivebound requests only the `openid`, `email`, and `profile` scopes; it does not request access to Google Drive or Google Photos.

## First-run onboarding

New accounts complete three steps:

1. Confirm the library display name.
2. Connect a read-only media folder or defer it.
3. Review protection-drive guidance and open the library.

The default import folder is `data/imports`, exposed inside the containers as `/data/imports`. Scanning creates database metadata and derivatives but never moves, renames, or deletes source files.

## Storage safety

The host directories are:

- `data/originals` for managed originals
- `data/derivatives` for generated thumbnails and previews
- `data/staging` for incomplete resumable transfers
- `data/replicas` for verified protection copies
- `data/backups` for incremental configuration and logical database archives

Originals are checksum-addressed and created without overwriting an existing path. The worker can write only to the managed originals and replica mounts so it can complete verified restores. Staged files are removed after ingestion succeeds or fails.

New uploads and imported media are automatically copied to the protection drive. Each replica is SHA-256 verified and receives a JSON metadata sidecar containing original names, paths, filesystem dates, capture time, GPS, camera/lens information, and embedded EXIF or container metadata. Restores verify the replica again, atomically replace the managed original, and reapply the preserved modification time.

Managed uploads and generated thumbnails are encrypted at rest with an account-specific AES-256-GCM key. The deployment master key wraps each account key and must come from a file-backed secret in staging and production. This deliberately preserves local preview, OCR, and future face processing; it is not a zero-knowledge mode. Set `MEDIA_ENCRYPTION_MIGRATE_LEGACY=true` only after taking a verified backup to migrate pre-encryption managed media in bounded background batches.

For real drive-failure protection, mount `/data/replicas` from a second physical drive. Keeping originals and replicas on the same disk verifies the workflow but does not protect against physical disk failure. To add further drives, mount each at a path in `REPLICA_ROOTS`, then add the approved container path in **Storage**. A policy selects the desired verified-copy count, drive priority, eligibility, balancing threshold, and retention period. A move is never destructive: the destination copy must checksum-verify before the source copy is released.

## Lifecycle, recovery, and backups

Files use immutable revisions. Moving an item to **Trash** records an audit event and a `purge_after` date; restore is available throughout the configured retention period. Rollback makes a previous revision active without overwriting it. The scheduled purge removes Drivebound-managed originals, derivatives, and replicas only after retention has expired and no other revision references those bytes. External libraries remain read-only and are never deleted by Drivebound.

The scheduler writes deduplicated configuration snapshots and periodic logical PostgreSQL archives under `BACKUPS_PATH`. Each database archive is checked with `pg_restore --list` before it counts as verified. Archives older than `DATABASE_BACKUP_RETENTION_DAYS` are pruned. Review their verification state in **Storage**.

`GET /api/v1/storage/recovery` provides an authenticated readiness summary without exposing host paths, checksums, or secrets. Account-scoped `POST /api/v1/monitoring/verify` performs a non-destructive checksum scan; `POST /api/v1/monitoring/run` also permits safe repair from verified copies. Follow the [recovery drill runbook](docs/operations/recovery-drill.md) for isolated PostgreSQL and media restore testing.

Deleting an account is separate from ordinary Trash retention. Drivebound keeps a crash-resumable deletion job and a pseudonymous suppression marker under `BACKUPS_PATH/account-deletion-suppressions`. The worker removes catalog-owned managed originals, derivatives, replicas, staging files, and account-scoped orphan bytes while never deleting external-library originals. Canonical cross-kind reference checks protect legacy aliases; unsafe or ambiguous paths stop in `manual_review` instead of being reported as erased. Historical archives remain subject to the suppression-ledger and retention procedure, so operators must still complete the live deletion-and-restore acceptance gate before making a production erasure claim.

## Browser upload

The web interface supports resumable concurrent uploads and automatically uses the signed-in session. To exercise the API manually, first save a login cookie:

```powershell
curl.exe -c drivebound.cookies -X POST http://localhost:8000/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d '{"email":"you@example.com","password":"your-long-password"}'

curl.exe -b drivebound.cookies -X POST http://localhost:8000/api/v1/assets/upload `
  -F "file=@C:\path\to\photo.jpg"
```

The response returns the asset immediately. `duplicate` is `true` when the same account has already ingested identical SHA-256 content. Metadata and thumbnails continue in the background.

## Timeline API

Authenticated timeline requests no longer include a user ID:

```text
GET /api/v1/assets?limit=100
GET /api/v1/assets?limit=100&cursor=NEXT_CURSOR
```

Items are ordered by `taken_at DESC`, falling back to `created_at`. Equal timestamps use the asset UUID as a stable tie-breaker.

## Desktop folder sync

The dependency-free desktop client synchronizes one explicitly selected folder at a time. It keeps `.drivebound-sync.json` with logical IDs, revisions, resumable upload state, and a server cursor. Renames are journaled as moves; deletion is an explicit tombstone operation; a simultaneous edit or delete/change is recorded as a conflict and neither local version is silently overwritten:

```powershell
python tools\drivebound_sync.py "D:\Pictures" `
  --server http://localhost:8000 `
  --token YOUR_ACCESS_TOKEN
```

Run the same command with `--root-id ROOT_ID` on a second computer to join an existing selected-folder root. The client first pulls acknowledged remote operations, then publishes local changes. It exits with status `2` when a conflict needs a choice in Drivebound, leaving both local data and the server revision intact.

Obtain a token from `POST /api/v1/auth/token`, using the account email as the OAuth `username`. Treat the token like a password until it expires.

## Mobile backup

The native Expo client lives in `mobile`. Install its packages with `npm install`, then use `npx expo run:android` or `npx expo run:ios`. Connect with the same Drivebound account used on the web. On a physical phone, enter the computer's LAN or HTTPS API address rather than `localhost`.

Each installation receives a separate revocable device credential. Camera-roll uploads resume in a device-scoped durable SQLite transfer queue with expired-lease recovery, bounded retry, and user-visible pause, cancel, and retry controls. The untouched bytes, filename, filesystem dates, capture time, EXIF, GPS, and video-container metadata are preserved. The app includes paginated library browsing, search, albums, memories, location discovery, authenticated sharing/downloads, video playback, original restore-to-phone, Wi-Fi/charging/bandwidth/schedule controls, and notification registration. The operating system schedules automatic backups; a native development/release build is required because background tasks do not run in Expo Go.

## Search intelligence and recovery

The worker runs Tesseract OCR for images and creates local semantic embeddings for natural-language search. The model cache stays in the `model_cache` volume. If model loading is unavailable, Drivebound uses a deterministic local fallback index rather than sending media or metadata to an external service.

The `scheduler` service checks SHA-256 integrity every 15 minutes by default. Missing or damaged managed originals are restored from verified protection copies; missing or damaged replicas are rebuilt from a healthy original. Events and manual checks are available in the Storage panel.

## Collaboration and storage nodes

Album owners can invite viewers or editors by email. Invitations are hashed, single-use, expire after seven days, and require the invited account's email. In console email mode the local invitation URL is displayed for development.

Storage nodes pair through a single-use 10-minute code. On the node host run:

```powershell
python tools\drivebound_node.py http://localhost:8000 --pair-code ABCD1234 --name "Basement drive"
```

`tools/drivebound_node.py` is the supported dependency-free Ed25519 node
client. The incompatible experimental standalone `node_service` package and
its installer wrappers were removed and must not be deployed.

The node generates a local Ed25519 identity, proves possession when claiming the
code, and signs every heartbeat. Its private key and returned node secret are
written atomically with restricted permissions under `%LOCALAPPDATA%\Drivebound`
on Windows or `$XDG_CONFIG_HOME/Drivebound` on Unix. Repository-local override
names are ignored by Git, but the default keeps credentials outside the project.
For a remote server, use HTTPS. Rotate the opaque node secret
without replacing its signing identity with:

```powershell
python tools\drivebound_node.py https://drivebound.example --rotate-secret --once
```

Nodes paired before attestation support was added are deliberately blocked. Give
the node a new pairing code and pair it again; the old record can then be revoked
from the Storage panel. See the [node attestation contract](docs/security/node-attestation.md)
for interoperability and recovery details.

## Secure remote access

The Docker ports bind to loopback by default. Put Drivebound behind a trusted HTTPS reverse proxy, VPN, or authenticated tunnel rather than forwarding ports 3000 or 8000 directly from a router. For internet access, set HTTPS `APP_URL`, `API_URL`, and `GOOGLE_REDIRECT_URI`, then set `REMOTE_ACCESS_ENABLED=true`, `AUTH_COOKIE_SECURE=true`, and list the public hostnames in `TRUSTED_HOSTS`. Startup is rejected when remote mode uses HTTP, wildcard CORS, or default/short secrets.

Remote deployments must also configure `METRICS_AUTH_TOKEN`; staging and production load it from `METRICS_AUTH_TOKEN_FILE`. Prometheus scrapers send it as a bearer token to `/metrics`. Keep the endpoint network-private even when token protection is enabled.

## Validation

Run the complete local release gate from PowerShell:

```powershell
.\tools\validate.ps1
```

Add `-Docker` to validate both Compose configurations and `-Android` to build the Android debug application with Android Studio's bundled JDK. CI runs backend tests, frontend lint/build, mobile type checking, Compose migrations, and guaranteed volume cleanup independently.

## Current product status

- **Account access:** registration, login, HTTP-only sessions, logout, onboarding, mandatory owner filtering, passkeys/recent authentication, and durable account-deletion intent are implemented. Production deletion acceptance still requires staging restore-replay and physical-erasure evidence.
- **Mobile experience:** a native Expo library, discovery, sharing, restore, SQLite-backed resumable transfer queue, policy controls, local/Expo push notifications, and OS-scheduled background backup are implemented.
- **Storage lifecycle:** multi-drive verified replica policies, safe balancing, immutable revisions, rollback, Trash retention, audited purge, and operational backup verification are implemented.
- **Desktop sync:** selective folder roots use durable local/server journals, explicit delete tombstones, resumable transfers, reconnect cursors, and visible conflict records.
- **Media grouping:** account-private perceptual near-duplicate candidates, RAW/JPEG, Live Photo/motion-photo, and burst groups are derived locally from metadata and image content.
- **Private sharing:** opaque, hashed, expiring links and optional Argon2 password protection are implemented.
- **Organization:** Files, collaborative Albums, OCR/semantic Search, Memories, and GPS Map screens are connected to permission-scoped APIs.
- **Operations:** scheduled integrity monitoring, account-scoped verification, multi-replica automatic repair, health events, and Ed25519-attested storage-node pairing/credential rotation are implemented.
- **Production foundation:** hybrid deployment configuration, file-backed production secrets, account-scoped media encryption, structured route-safe request logging, bearer-protected Prometheus metrics, dependency update automation, and CI/release-candidate workflows are implemented.
- **Interface:** registration, onboarding, library, storage, uploads, viewer, and public sharing use the centralized neumorphic design system.

## Storage health scope

The Storage panel reports availability, writability, capacity, free space, and utilization. Container isolation does not safely expose physical SMART data, so SMART is explicitly reported as unavailable. A future host-level adapter can add temperature and predictive health without granting the application privileged device access.
