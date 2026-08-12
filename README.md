# Drivebound

Drivebound is an expandable personal cloud backed by drives you own. It provides account-isolated photo libraries, resumable checksum-first uploads, background media processing, storage expansion, verified protection copies, private sharing, albums, search, and map discovery.

## Start

1. Copy `.env.example` to `.env`.
2. Change `POSTGRES_PASSWORD` in both `POSTGRES_PASSWORD` and `DATABASE_URL`.
3. Replace `JWT_SECRET` with a long random value.
4. Run `docker compose up --build -d`.
5. Open <http://localhost:3000>, create an account, and complete setup.

The backend applies Alembic migrations through `0013` before starting. Confirm the current migration with:

```text
docker compose exec backend alembic current
```

Service health is available at <http://localhost:8000/api/v1/health>. Interactive API documentation is available at <http://localhost:8000/docs>.

## Accounts and ownership

Registration, login, logout, current-user lookup, and onboarding are implemented. Browser sessions use an HTTP-only, same-site cookie. Private routes derive ownership from the authenticated account rather than trusting a browser-supplied user ID.

Assets, resumable uploads, libraries, devices, albums, storage status, protection actions, and share creation are account-scoped. Duplicate detection is scoped per account so one user cannot discover another user's files through checksum responses.

Email verification, password recovery and changes, rotating revocable refresh sessions, active-device controls, security history, Redis-backed login throttling, authenticator-app MFA with one-use recovery codes, account export/deletion, and a trusted-host recovery command are implemented. Before broad internet exposure, configure SMTP and a trusted HTTPS ingress, replace both secrets, and set `AUTH_COOKIE_SECURE=true`.

Local development uses `EMAIL_DELIVERY_MODE=console`; verification and reset screens expose their development-only links. Production must use `EMAIL_DELIVERY_MODE=smtp` and configure `SMTP_HOST`, `SMTP_FROM`, and any required credentials. Administrative recovery is deliberately not exposed over HTTP; run `.venv\Scripts\python.exe tools\account_recovery.py --help` from the project folder on a trusted host.

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

Originals are checksum-addressed and created without overwriting an existing path. The worker can write only to the managed originals and replica mounts so it can complete verified restores. Staged files are removed after ingestion succeeds or fails.

New uploads and imported media are automatically copied to the protection drive. Each replica is SHA-256 verified and receives a JSON metadata sidecar containing original names, paths, filesystem dates, capture time, GPS, camera/lens information, and embedded EXIF or container metadata. Restores verify the replica again, atomically replace the managed original, and reapply the preserved modification time.

Managed uploads and generated thumbnails are encrypted at rest with an account-specific AES-256-GCM key. The deployment master key wraps each account key and must come from a file-backed secret in staging and production. This deliberately preserves local preview, OCR, and future face processing; it is not a zero-knowledge mode. Set `MEDIA_ENCRYPTION_MIGRATE_LEGACY=true` only after taking a verified backup to migrate pre-encryption managed media in bounded background batches.

For real drive-failure protection, mount `/data/replicas` from a second physical drive. Keeping originals and replicas on the same disk verifies the workflow but does not protect against physical disk failure.

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

The dependency-free desktop client reads source files without modifying them and keeps a small local resume journal:

```powershell
python tools\drivebound_sync.py "D:\Pictures" `
  --server http://localhost:8000 `
  --token YOUR_ACCESS_TOKEN
```

Obtain a token from `POST /api/v1/auth/token`, using the account email as the OAuth `username`. Treat the token like a password until it expires.

## Mobile backup

The native Expo client lives in `mobile`. Install its packages with `npm install`, then use `npx expo run:android` or `npx expo run:ios`. Connect with the same Drivebound account used on the web. On a physical phone, enter the computer's LAN or HTTPS API address rather than `localhost`.

Each installation receives a separate revocable device credential. Camera-roll uploads resume in a durable SQLite transfer queue and preserve the untouched bytes, filename, filesystem dates, capture time, EXIF, GPS, and video-container metadata. The app includes library browsing, search, albums, memories, location discovery, sharing, video playback, original restore-to-phone, Wi-Fi/charging/bandwidth/schedule controls, and notification registration. The operating system schedules automatic backups; a native development/release build is required because background tasks do not run in Expo Go.

## Search intelligence and recovery

The worker runs Tesseract OCR for images and creates local semantic embeddings for natural-language search. The model cache stays in the `model_cache` volume. If model loading is unavailable, Drivebound uses a deterministic local fallback index rather than sending media or metadata to an external service.

The `scheduler` service checks SHA-256 integrity every 15 minutes by default. Missing or damaged managed originals are restored from verified protection copies; missing or damaged replicas are rebuilt from a healthy original. Events and manual checks are available in the Storage panel.

## Collaboration and storage nodes

Album owners can invite viewers or editors by email. Invitations are hashed, single-use, expire after seven days, and require the invited account's email. In console email mode the local invitation URL is displayed for development.

Storage nodes pair through a single-use 10-minute code. On the node host run:

```powershell
python tools\drivebound_node.py http://localhost:8000 --pair-code ABCD1234 --name "Basement drive"
```

The returned secret is stored only on that node and can be revoked from the web Storage panel.

## Secure remote access

The Docker ports bind to loopback by default. Put Drivebound behind a trusted HTTPS reverse proxy, VPN, or authenticated tunnel rather than forwarding ports 3000 or 8000 directly from a router. For internet access, set HTTPS `APP_URL`, `API_URL`, and `GOOGLE_REDIRECT_URI`, then set `REMOTE_ACCESS_ENABLED=true`, `AUTH_COOKIE_SECURE=true`, and list the public hostnames in `TRUSTED_HOSTS`. Startup is rejected when remote mode uses HTTP, wildcard CORS, or default/short secrets.

## Current product status

- **Account access:** registration, login, HTTP-only sessions, logout, onboarding, and mandatory owner filtering are implemented.
- **Mobile experience:** a native Expo library, discovery, sharing, restore, SQLite-backed resumable transfer queue, policy controls, local/Expo push notifications, and OS-scheduled background backup are implemented.
- **Protection:** automatic verified copies, metadata sidecars, bulk protection status, and checksum-verified restore are implemented under `/data/replicas`.
- **Private sharing:** opaque, hashed, expiring links and optional Argon2 password protection are implemented.
- **Organization:** Files, collaborative Albums, OCR/semantic Search, Memories, and GPS Map screens are connected to permission-scoped APIs.
- **Operations:** scheduled integrity monitoring, automatic original/replica repair, health events, and hosted node pairing are implemented.
- **Production foundation:** hybrid deployment configuration, file-backed production secrets, account-scoped media encryption, structured request logging, Prometheus metrics, and CI/release-candidate workflows are implemented.
- **Interface:** registration, onboarding, library, storage, uploads, viewer, and public sharing use the centralized neumorphic design system.

## Storage health scope

The Storage panel reports availability, writability, capacity, free space, and utilization. Container isolation does not safely expose physical SMART data, so SMART is explicitly reported as unavailable. A future host-level adapter can add temperature and predictive health without granting the application privileged device access.
