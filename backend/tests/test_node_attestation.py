import asyncio
import importlib.util
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from app.api.v1.nodes import (
    claim_node,
    heartbeat,
    node_from_secret,
    rotate_node_secret,
    router,
    verify_attested_request,
)
from app.core.security import token_digest
from app.models.node import PairedNode
from app.schemas.node import NodeClaim, NodeHeartbeat, NodeSecretRotation
from app.services.node_attestation import (
    MAX_ATTESTATION_AGE_MS,
    MAX_ATTESTATION_FUTURE_MS,
    NodeAttestationError,
    claim_payload,
    encode_base64url,
    heartbeat_payload,
    rotation_payload,
    validate_public_key,
    validate_timestamp,
    verify_signature,
)


def _load_node_client():
    path = Path(__file__).resolve().parents[2] / "tools" / "drivebound_node.py"
    spec = importlib.util.spec_from_file_location("drivebound_node_client", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


node_client = _load_node_client()


def _signature_placeholder() -> str:
    return encode_base64url(b"\0" * 64)


def _attested_node(public_key: str, *, node_id: uuid.UUID | None = None) -> PairedNode:
    return PairedNode(
        id=node_id or uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="Test drive",
        public_key=public_key,
        secret_hash=token_digest("old-node-token"),
        status="online",
        attestation_state="verified",
    )


class _NodeSession:
    def __init__(self, node: PairedNode):
        self.node = node
        self.statement = None
        self.commits = 0

    async def scalar(self, statement):
        self.statement = statement
        requested_digest = next(
            (value for value in statement.compile().params.values() if isinstance(value, str) and len(value) == 64),
            None,
        )
        return self.node if requested_digest == self.node.secret_hash else None

    async def commit(self):
        self.commits += 1


class _ClaimSession:
    def __init__(self):
        self.pairing = SimpleNamespace(user_id=uuid.uuid4(), claimed_at=None)
        self.node: PairedNode | None = None
        self.commits = 0

    async def scalar(self, statement):
        return self.pairing

    def add(self, node: PairedNode):
        self.node = node

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("A valid claim must not roll back")

    async def refresh(self, node: PairedNode):
        if node.id is None:
            node.id = uuid.uuid4()


def test_node_client_and_server_use_the_same_canonical_signed_payloads() -> None:
    private_key, public_key = node_client.generate_identity()
    node_id = uuid.uuid4()
    timestamp_ms = int(time.time() * 1000)
    capabilities = {"backup": True, "storage": {"volumes": 2}}

    client_heartbeat = node_client.heartbeat_payload(
        str(node_id), timestamp_ms, node_client.NODE_VERSION, capabilities
    )
    server_heartbeat = heartbeat_payload(node_id, timestamp_ms, node_client.NODE_VERSION, capabilities)
    assert client_heartbeat == server_heartbeat
    verify_signature(public_key, node_client.sign(private_key, client_heartbeat), server_heartbeat)

    # The bundled client does not advertise a callback endpoint. Both sides
    # therefore sign an explicit JSON null rather than different empty forms.
    client_claim = node_client.claim_payload("ab12cd34", "Office drive", public_key, timestamp_ms, None)
    server_claim = claim_payload("AB12CD34", "Office drive", public_key, timestamp_ms, None)
    assert client_claim == server_claim
    verify_signature(public_key, node_client.sign(private_key, client_claim), server_claim)

    client_rotation = node_client.rotation_payload(str(node_id), timestamp_ms + 1)
    server_rotation = rotation_payload(node_id, timestamp_ms + 1)
    assert client_rotation == server_rotation
    verify_signature(public_key, node_client.sign(private_key, client_rotation), server_rotation)


@pytest.mark.parametrize(
    ("version", "capabilities", "timestamp_delta", "use_other_node"),
    [
        ("tampered", {"backup": True}, 0, False),
        ("0.2.0", {"backup": False}, 0, False),
        ("0.2.0", {"backup": True}, 1, False),
        ("0.2.0", {"backup": True}, 0, True),
    ],
)
def test_heartbeat_signature_covers_every_mutable_field(
    version: str,
    capabilities: dict[str, object],
    timestamp_delta: int,
    use_other_node: bool,
) -> None:
    private_key, public_key = node_client.generate_identity()
    node_id = uuid.uuid4()
    timestamp_ms = int(time.time() * 1000)
    signature = node_client.sign(
        private_key,
        node_client.heartbeat_payload(str(node_id), timestamp_ms, "0.2.0", {"backup": True}),
    )
    changed_node_id = uuid.uuid4() if use_other_node else node_id
    with pytest.raises(NodeAttestationError, match="signature"):
        verify_signature(
            public_key,
            signature,
            heartbeat_payload(changed_node_id, timestamp_ms + timestamp_delta, version, capabilities),
        )


def test_node_client_detects_a_private_public_identity_mismatch() -> None:
    first_private, _ = node_client.generate_identity()
    _, second_public = node_client.generate_identity()
    assert node_client.public_key_for_private(first_private) != second_public


def test_node_client_rejects_mismatched_identity_in_config(monkeypatch, tmp_path: Path) -> None:
    first_private, _ = node_client.generate_identity()
    _, second_public = node_client.generate_identity()
    path = tmp_path / "node.json"
    path.write_text(
        node_client.json.dumps(
            {
                "server": "http://localhost:8000",
                "node_id": str(uuid.uuid4()),
                "node_secret": "secret",
                "private_key": first_private,
                "public_key": second_public,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(node_client, "prepare_config_directory", lambda _: None)
    monkeypatch.setattr(node_client, "restrict_config_file", lambda _: None)
    with pytest.raises(RuntimeError, match="does not match"):
        node_client.read_config(path)


def test_node_client_requires_https_outside_loopback() -> None:
    assert node_client.validate_server_url("http://localhost:8000") == "http://localhost:8000"
    assert node_client.validate_server_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert node_client.validate_server_url("https://drivebound.example/") == "https://drivebound.example"
    with pytest.raises(ValueError, match="HTTPS"):
        node_client.validate_server_url("http://192.168.1.5:8000")


def test_node_client_never_forwards_credentials_through_cross_origin_redirects() -> None:
    request = node_client.urllib.request.Request("https://drivebound.example/api/v1/nodes/heartbeat")
    handler = node_client.RejectRedirectHandler()
    with pytest.raises(node_client.urllib.error.HTTPError, match="do not follow redirects"):
        handler.redirect_request(
            request,
            None,
            307,
            "Temporary Redirect",
            {},
            "https://attacker.example/collect",
        )


def test_public_keys_require_canonical_raw_ed25519_material() -> None:
    _, public_key = node_client.generate_identity()
    assert validate_public_key(public_key) == public_key
    with pytest.raises(NodeAttestationError):
        validate_public_key(public_key + "=")
    with pytest.raises(NodeAttestationError):
        validate_public_key("legacy-random-public-key")
    with pytest.raises(NodeAttestationError):
        verify_signature(public_key, "not-a-signature", b"message")


def test_attestation_timestamps_reject_stale_future_and_replayed_messages() -> None:
    now_ms = 2_000_000_000_000
    validate_timestamp(now_ms, now_ms=now_ms)
    with pytest.raises(NodeAttestationError, match="stale"):
        validate_timestamp(now_ms - MAX_ATTESTATION_AGE_MS - 1, now_ms=now_ms)
    with pytest.raises(NodeAttestationError, match="future"):
        validate_timestamp(now_ms + MAX_ATTESTATION_FUTURE_MS + 1, now_ms=now_ms)
    with pytest.raises(NodeAttestationError, match="already used"):
        validate_timestamp(now_ms, last_timestamp_ms=now_ms, now_ms=now_ms)


@pytest.mark.parametrize(
    "capabilities",
    [
        {f"key-{index}": True for index in range(65)},
        {"description": "x" * (17 * 1024)},
        {"values": list(range(257))},
        {"not-finite": float("nan")},
    ],
)
def test_heartbeat_capabilities_are_bounded(capabilities: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NodeHeartbeat(
            node_id=uuid.uuid4(),
            timestamp_ms=1,
            capabilities=capabilities,
            signature=_signature_placeholder(),
        )


def test_heartbeat_capability_boundaries_are_accepted() -> None:
    exact_size_value = "x" * (16 * 1024 - len('{"value":""}'))
    accepted = [
        {f"key-{index}": True for index in range(64)},
        {"value": exact_size_value},
        {"values": list(range(255))},  # root key + 255 array members
    ]
    nested: dict[str, object] | bool = True
    for _ in range(8):
        nested = {"nested": nested}
    accepted.append(nested)
    for capabilities in accepted:
        assert NodeHeartbeat(
            node_id=uuid.uuid4(),
            timestamp_ms=1,
            capabilities=capabilities,
            signature=_signature_placeholder(),
        ).capabilities == capabilities


def test_heartbeat_rejects_one_byte_or_member_over_the_limit() -> None:
    exact_size_value = "x" * (16 * 1024 - len('{"value":""}'))
    for capabilities in ({"value": exact_size_value + "x"}, {"values": list(range(256))}):
        with pytest.raises(ValidationError):
            NodeHeartbeat(
                node_id=uuid.uuid4(),
                timestamp_ms=1,
                capabilities=capabilities,
                signature=_signature_placeholder(),
            )


def test_heartbeat_capabilities_reject_excessive_nesting() -> None:
    capabilities: dict[str, object] = {"value": True}
    for _ in range(10):
        capabilities = {"nested": capabilities}
    with pytest.raises(ValidationError, match="nested"):
        NodeHeartbeat(
            node_id=uuid.uuid4(),
            timestamp_ms=1,
            capabilities=capabilities,
            signature=_signature_placeholder(),
        )


def test_valid_signed_claim_persists_a_verified_raw_identity() -> None:
    private_key, public_key = node_client.generate_identity()
    timestamp_ms = int(time.time() * 1000)
    message = claim_payload("AB12CD34", "Office drive", public_key, timestamp_ms, None)
    payload = NodeClaim(
        code="ab12cd34",
        name="Office drive",
        public_key=public_key,
        timestamp_ms=timestamp_ms,
        signature=node_client.sign(private_key, message),
    )
    session = _ClaimSession()

    response = Response()
    registration = asyncio.run(claim_node(payload, response, session))

    assert session.node is not None
    assert session.node.public_key == public_key
    assert session.node.attestation_state == "verified"
    assert session.node.last_attestation_timestamp_ms == timestamp_ms
    assert session.node.secret_hash == token_digest(registration.node_secret)
    assert session.pairing.claimed_at is not None
    assert registration.attestation_state == "verified"
    assert session.commits == 1
    assert response.headers["cache-control"] == "no-store"


def test_legacy_node_is_blocked_before_any_heartbeat_mutation() -> None:
    node_id = uuid.uuid4()
    node = SimpleNamespace(
        id=node_id,
        attestation_state="legacy_repair_required",
        public_key="legacy-random-value",
        last_attestation_timestamp_ms=None,
    )
    with pytest.raises(HTTPException) as exc:
        verify_attested_request(
            node,
            node_id=node_id,
            timestamp_ms=int(time.time() * 1000),
            signature=_signature_placeholder(),
            message=b"unused",
            now=datetime.now(timezone.utc),
        )
    assert exc.value.status_code == 409
    assert "re-paired" in exc.value.detail


def test_signed_heartbeat_is_monotonic_and_row_locked() -> None:
    private_key, public_key = node_client.generate_identity()
    node = _attested_node(public_key)
    session = _NodeSession(node)
    timestamp_ms = int(time.time() * 1000)
    capabilities = {"backup": True}
    payload = NodeHeartbeat(
        node_id=node.id,
        timestamp_ms=timestamp_ms,
        version="0.2.0",
        capabilities=capabilities,
        signature=node_client.sign(
            private_key,
            heartbeat_payload(node.id, timestamp_ms, "0.2.0", capabilities),
        ),
    )

    asyncio.run(heartbeat(payload, "old-node-token", session))
    assert session.statement._for_update_arg is not None
    assert node.last_attestation_timestamp_ms == timestamp_ms
    assert node.capabilities == capabilities
    assert session.commits == 1

    with pytest.raises(HTTPException) as replay:
        asyncio.run(heartbeat(payload, "old-node-token", session))
    assert replay.value.status_code == 409
    assert session.commits == 1


def test_concurrent_same_timestamp_heartbeats_allow_one_commit() -> None:
    private_key, public_key = node_client.generate_identity()
    node = _attested_node(public_key)
    timestamp_ms = int(time.time() * 1000)
    capabilities = {"backup": True}
    payload = NodeHeartbeat(
        node_id=node.id,
        timestamp_ms=timestamp_ms,
        version="0.2.0",
        capabilities=capabilities,
        signature=node_client.sign(
            private_key,
            heartbeat_payload(node.id, timestamp_ms, "0.2.0", capabilities),
        ),
    )
    row_lock = asyncio.Lock()

    class LockedSession(_NodeSession):
        async def scalar(self, statement):
            await row_lock.acquire()
            return await super().scalar(statement)

        async def commit(self):
            await super().commit()
            row_lock.release()

    first = LockedSession(node)
    second = LockedSession(node)

    async def compete():
        return await asyncio.gather(
            heartbeat(payload, "old-node-token", first),
            heartbeat(payload, "old-node-token", second),
            return_exceptions=True,
        )

    results = asyncio.run(compete())
    assert sum(result is None for result in results) == 1
    conflicts = [result for result in results if isinstance(result, HTTPException)]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert first.commits + second.commits == 1


def test_signed_rotation_atomically_replaces_the_opaque_node_secret() -> None:
    private_key, public_key = node_client.generate_identity()
    node = _attested_node(public_key)
    session = _NodeSession(node)
    timestamp_ms = int(time.time() * 1000)
    payload = NodeSecretRotation(
        node_id=node.id,
        timestamp_ms=timestamp_ms,
        signature=node_client.sign(private_key, rotation_payload(node.id, timestamp_ms)),
    )

    response = Response()
    result = asyncio.run(rotate_node_secret(payload, response, "old-node-token", session))

    assert session.statement._for_update_arg is not None
    assert node.secret_hash != token_digest("old-node-token")
    assert node.secret_hash == token_digest(result.node_secret)
    assert node.secret_rotated_at == result.rotated_at
    assert node.last_attestation_timestamp_ms == timestamp_ms
    assert session.commits == 1
    assert response.headers["cache-control"] == "no-store"
    with pytest.raises(HTTPException) as old_token:
        asyncio.run(node_from_secret(session, "old-node-token"))
    assert old_token.value.status_code == 401
    assert asyncio.run(node_from_secret(session, result.node_secret)) is node


def test_owner_contract_exposes_attestation_without_private_material() -> None:
    paths = {route.path for route in router.routes}
    assert "/nodes/rotate-secret" in paths
    columns = PairedNode.__table__.columns
    indexes = {index.name for index in PairedNode.__table__.indexes}
    assert "last_attested_at" in columns
    assert "secret_rotated_at" in columns
    assert "uq_paired_nodes_active_attested_public_key" in indexes
