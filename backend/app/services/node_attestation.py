"""Canonical Ed25519 attestation messages for storage-node identity."""

import base64
import json
import time
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64
MAX_ATTESTATION_AGE_MS = 5 * 60 * 1000
MAX_ATTESTATION_FUTURE_MS = 30 * 1000


class NodeAttestationError(ValueError):
    """Raised when node identity material or a signed message is invalid."""


def encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_base64url(value: str, *, expected_bytes: int, label: str) -> bytes:
    try:
        if not value or "=" in value:
            raise ValueError
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise NodeAttestationError(f"{label} must be canonical unpadded base64url") from exc
    if len(decoded) != expected_bytes or encode_base64url(decoded) != value:
        raise NodeAttestationError(f"{label} has an invalid Ed25519 length or encoding")
    return decoded


def canonical_payload(action: str, **fields: Any) -> bytes:
    document = {"action": action, **fields}
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NodeAttestationError("Attestation payload contains unsupported JSON values") from exc


def heartbeat_payload(
    node_id: uuid.UUID,
    timestamp_ms: int,
    version: str | None,
    capabilities: dict[str, object],
) -> bytes:
    return canonical_payload(
        "heartbeat",
        node_id=str(node_id),
        timestamp_ms=timestamp_ms,
        version=version,
        capabilities=capabilities,
    )


def claim_payload(
    code: str,
    name: str,
    public_key: str,
    timestamp_ms: int,
    endpoint_url: str | None,
) -> bytes:
    return canonical_payload(
        "claim",
        code=code.upper(),
        name=name,
        public_key=public_key,
        timestamp_ms=timestamp_ms,
        endpoint_url=endpoint_url,
    )


def rotation_payload(node_id: uuid.UUID, timestamp_ms: int) -> bytes:
    return canonical_payload("rotate_secret", node_id=str(node_id), timestamp_ms=timestamp_ms)


def validate_timestamp(
    timestamp_ms: int,
    *,
    last_timestamp_ms: int | None = None,
    now_ms: int | None = None,
) -> None:
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if timestamp_ms < current - MAX_ATTESTATION_AGE_MS:
        raise NodeAttestationError("Attestation timestamp is stale")
    if timestamp_ms > current + MAX_ATTESTATION_FUTURE_MS:
        raise NodeAttestationError("Attestation timestamp is too far in the future")
    if last_timestamp_ms is not None and timestamp_ms <= last_timestamp_ms:
        raise NodeAttestationError("Attestation timestamp was already used")


def verify_signature(public_key: str, signature: str, message: bytes) -> None:
    raw_key = decode_base64url(public_key, expected_bytes=PUBLIC_KEY_BYTES, label="public_key")
    raw_signature = decode_base64url(signature, expected_bytes=SIGNATURE_BYTES, label="signature")
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_signature, message)
    except (InvalidSignature, ValueError) as exc:
        raise NodeAttestationError("Ed25519 signature verification failed") from exc


def validate_public_key(public_key: str) -> str:
    raw_key = decode_base64url(public_key, expected_bytes=PUBLIC_KEY_BYTES, label="public_key")
    # Constructing the key rejects invalid library-level encodings. Returning
    # our encoding ensures the database never contains alternate spellings.
    Ed25519PublicKey.from_public_bytes(raw_key)
    return encode_base64url(raw_key)
