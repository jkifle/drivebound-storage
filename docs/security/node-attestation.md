# Storage-node attestation contract

Drivebound storage nodes use two independent credentials. The server issues an
opaque node secret for authentication, while the node generates an Ed25519 key
pair locally for attestation. The server stores only the SHA-256 digest of the
opaque secret and the raw 32-byte public key encoded as canonical, unpadded
base64url. The private key never leaves the node configuration file.

## Pairing

An account owner creates a single-use pairing code. The node generates a new
Ed25519 identity and signs a canonical claim containing `action`, the uppercase
`code`, `name`, `public_key`, `timestamp_ms`, and `endpoint_url`. The bundled
client does not advertise a callback URL, so it signs and sends an explicit JSON
`null` for `endpoint_url`.

Custom clients that send an endpoint must first normalize it exactly as
Pydantic's `AnyHttpUrl` representation used by the API (including the normalized
trailing slash), then sign and send that same string. Remote callback endpoints
must use HTTPS.

Every signed document is UTF-8 JSON with keys sorted, no insignificant
whitespace, non-ASCII characters preserved, non-finite numbers rejected, and
these separators:

```python
json.dumps(document, sort_keys=True, separators=(",", ":"),
           ensure_ascii=False, allow_nan=False).encode("utf-8")
```

The claim response returns the opaque node secret once and carries
`Cache-Control: no-store`.

## Heartbeats and replay protection

`POST /api/v1/nodes/heartbeat` requires `X-Node-Token` plus a signature over
`action="heartbeat"`, `node_id`, the monotonic millisecond `timestamp_ms`,
`version`, and `capabilities`. Under a database row lock, the server rejects an
ID mismatch, bad signature, timestamp reuse, messages older than five minutes,
and messages more than 30 seconds in the future before changing node state.

Capabilities are constrained before persistence: at most 64 top-level keys,
256 total object/array members, eight levels of nesting, and 16 KiB of canonical
JSON. Values must be ordinary finite JSON values. Owners can see the node's
attestation state and last-attested time, but never its token digest or public
host filesystem information.

## Secret rotation

`POST /api/v1/nodes/rotate-secret` requires the current node token and a signed
canonical document containing `action="rotate_secret"`, `node_id`, and a fresh
monotonic `timestamp_ms`. The server locks the node row, replaces the stored
digest in the same transaction, and returns the replacement secret once with
`Cache-Control: no-store`. The old secret stops working as soon as the transaction
commits.

The node reserves and saves a timestamp before sending each signed request. If a
rotation response is lost after the server commits, neither the old secret nor a
secret absent from the local configuration can recover the session. Create a new
pairing code as the signed-in owner and explicitly re-pair the node.

## Legacy nodes and local key handling

Migration 0015 marks every existing node `legacy_repair_required`; older clients
stored a random public-looking string and cannot be upgraded into a trusted
identity. Heartbeats from those records receive HTTP 409 until explicit
re-pairing. There is no silent trust transition.

The bundled client atomically writes its configuration with mode `0600` on
POSIX. On Windows it removes inherited ACLs and grants access only to the current
user and `SYSTEM`; its default per-user configuration directory is restricted
before credential bytes are written, and the ACL is reapplied when reading. It
also checks that the stored public key is derived from the stored private key.
Back up this file only to an access-controlled secret store. Use HTTPS for every
non-loopback API URL. The client refuses all HTTP redirects so a node token or
signed request cannot be forwarded to another origin.

Node attestation authorizes status/capability reporting and credential rotation
only. It does not provide remote command execution, remote software installation,
or signed software updating.
