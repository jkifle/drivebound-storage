# Phase 0: hybrid foundation

## Product mode

Drivebound is **hybrid**. The hosted control plane provides account routing,
node pairing, device notifications, relay coordination, releases, and service
status. The data plane remains flexible: a member can store media on their own
node, Drivebound-hosted storage, or both. This keeps a low-cost self-hosted
entry point while allowing reliable remote access and future hosted plans.

The control plane must not become the only copy of a member's media. A node
remains usable on the local network during a control-plane outage and later
reconciles signed operations.

## Encryption decision

Drivebound uses **server-assisted per-user encryption at rest**, not
zero-knowledge encryption. It preserves previews, OCR, face recognition, video
transcoding, recovery, and malware scanning without moving raw media to a
third-party AI service.

- Every account receives a random 256-bit media data-encryption key (DEK).
- The DEK is wrapped with a deployment-managed Fernet master key and stored in
  `users.media_key_encrypted`; the raw DEK is never stored in PostgreSQL.
- New managed originals and thumbnails use AES-256-GCM. Their encrypted paths
  include the account ID, so ciphertext is never shared across accounts.
- Replicas copy encrypted bytes and verify `storage_checksum`; plaintext
  `checksum` remains an internal, account-scoped deduplication value.
- Workers decrypt to a short-lived staging file only while extracting metadata,
  generating a preview, or running local intelligence. The temporary file is
  removed when the task exits.
- Existing files are marked `encryption_version=0`. Enable
  `MEDIA_ENCRYPTION_MIGRATE_LEGACY=true` to migrate a small bounded batch each
  minute. It encrypts the original and thumbnail, re-queues protection, and
  removes a stale plaintext protection copy only after its encrypted
  replacement is checksum-verified.

This model protects drives, backups, and lost storage media. It does **not**
protect media from a compromised running Drivebound service with access to its
master key. A future zero-knowledge vault must be an explicit opt-in mode with
client-side preview/search/AI limitations.

### Key operation rules

- Development can use the intentionally public development key only for local
  data. It is rejected in staging and production.
- Staging and production use `MEDIA_ENCRYPTION_MASTER_KEY_FILE`, mounted from a
  secret manager; `.env` is never a production secret source.
- Rotate a master key by rewrapping account DEKs in a maintenance job. Rotate
  an account DEK by re-encrypting that account's media and derivatives.
- Deleting an account's wrapped DEK after its verified retention window is the
  final cryptographic deletion step.

## Data lifecycle contract

The canonical lifecycle is defined before implementing trash/versioning:

`active → superseded (optional) → trashed → retained → purged`

- An edit/replacement creates a new immutable version; clients never mutate an
  original in place.
- A deletion is a tombstone with `trashed_at`, `purge_after`, and an actor/event
  record. It remains reversible through the configured retention period.
- Sync deletion propagates only a tombstone bearing the same logical item ID;
  it never uses a missing file as proof of deletion.
- Purge removes derivatives, replicas, and wrapped keys only after the
  retention period and replica/index jobs finish successfully.

## Sync and conflict contract

Folder synchronization will use a per-root change journal with stable logical
IDs, revision IDs, device IDs, and vector-clock-like parent revisions.

- Files are immutable versions; rename and move preserve the logical ID.
- A concurrent edit creates two versions and a visible conflict, never silent
  last-writer-wins replacement.
- A device reconnects by submitting its journal since the last acknowledged
  cursor. The server returns missing remote operations and unresolved conflicts.
- A deletion conflicts with a concurrent modification; the modification is
  retained and surfaced for a user decision.

## Node protocol contract

Nodes have a public key, a revocable device secret, capability document,
semantic version, and monotonically increasing operation sequence.

- Pairing is single-use and time-limited.
- All control-plane commands are signed, include an expiry and command ID, and
  are acknowledged idempotently.
- Nodes persist an outbound operation log while offline and reconcile by cursor
  after reconnecting.
- Nodes never accept owner transfer, key changes, or destructive storage
  commands without a separately signed recovery/approval action.
