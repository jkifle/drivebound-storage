import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user, opaque_token, token_digest
from app.db.session import get_db
from app.models.node import NodePairingCode, PairedNode
from app.models.user import User
from app.schemas.node import (
    NodeClaim,
    NodeHeartbeat,
    NodeRegistration,
    NodeResponse,
    NodeSecretRotation,
    NodeSecretRotationResponse,
    PairingCodeResponse,
)
from app.services.node_attestation import (
    NodeAttestationError,
    claim_payload,
    heartbeat_payload,
    rotation_payload,
    validate_public_key,
    validate_timestamp,
    verify_signature,
)

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post("/pairing-code", response_model=PairingCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_pairing_code(
    response: Response,
    session: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
) -> PairingCodeResponse:
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.node_pairing_minutes)
    session.add(NodePairingCode(user_id=user.id, code_hash=token_digest(code), expires_at=expires))
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return PairingCodeResponse(code=code, expires_at=expires)


@router.post("/claim", response_model=NodeRegistration, status_code=status.HTTP_201_CREATED)
async def claim_node(
    payload: NodeClaim,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> NodeRegistration:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    code = payload.code.upper()
    endpoint_url = str(payload.endpoint_url) if payload.endpoint_url else None
    try:
        public_key = validate_public_key(payload.public_key)
        validate_timestamp(payload.timestamp_ms, now_ms=now_ms)
        verify_signature(
            public_key,
            payload.signature,
            claim_payload(code, payload.name, public_key, payload.timestamp_ms, endpoint_url),
        )
    except NodeAttestationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    pairing = await session.scalar(select(NodePairingCode).where(
        NodePairingCode.code_hash == token_digest(code),
        NodePairingCode.claimed_at.is_(None), NodePairingCode.expires_at > now,
    ).with_for_update())
    if pairing is None:
        raise HTTPException(status_code=400, detail="Pairing code is invalid or expired")
    if settings.remote_access_enabled and payload.endpoint_url and payload.endpoint_url.scheme != "https":
        raise HTTPException(status_code=400, detail="Remote nodes require an HTTPS endpoint")
    secret = opaque_token(48)
    node = PairedNode(
        user_id=pairing.user_id, name=payload.name, public_key=public_key,
        endpoint_url=endpoint_url,
        secret_hash=token_digest(secret), status="online", last_seen_at=now,
        attestation_state="verified", last_attested_at=now,
        last_attestation_timestamp_ms=payload.timestamp_ms,
    )
    pairing.claimed_at = now
    session.add(node)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="This node identity is already paired") from exc
    await session.refresh(node)
    response.headers["Cache-Control"] = "no-store"
    return NodeRegistration(node_id=node.id, node_secret=secret, attestation_state=node.attestation_state)


async def node_from_secret(session: AsyncSession, secret: str | None, *, lock: bool = False) -> PairedNode:
    if not secret:
        raise HTTPException(status_code=401, detail="X-Node-Token is required")
    statement = select(PairedNode).where(
        PairedNode.secret_hash == token_digest(secret),
        PairedNode.revoked_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    node = await session.scalar(statement)
    if node is None:
        raise HTTPException(status_code=401, detail="Invalid node token")
    return node


def verify_attested_request(
    node: PairedNode,
    *,
    node_id: uuid.UUID,
    timestamp_ms: int,
    signature: str,
    message: bytes,
    now: datetime,
) -> None:
    if node.attestation_state != "verified":
        raise HTTPException(status_code=409, detail="Legacy node must be re-paired before signed requests are accepted")
    if node_id != node.id:
        raise HTTPException(status_code=403, detail="Signed node_id does not match the authenticated node")
    try:
        validate_timestamp(
            timestamp_ms,
            last_timestamp_ms=node.last_attestation_timestamp_ms,
            now_ms=int(now.timestamp() * 1000),
        )
    except NodeAttestationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        verify_signature(node.public_key, signature, message)
    except NodeAttestationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(payload: NodeHeartbeat, x_node_token: str | None = Header(default=None), session: AsyncSession = Depends(get_db)) -> None:
    node = await node_from_secret(session, x_node_token, lock=True)
    now = datetime.now(timezone.utc)
    verify_attested_request(
        node,
        node_id=payload.node_id,
        timestamp_ms=payload.timestamp_ms,
        signature=payload.signature,
        message=heartbeat_payload(payload.node_id, payload.timestamp_ms, payload.version, payload.capabilities),
        now=now,
    )
    node.last_seen_at = now
    node.last_attested_at = now
    node.last_attestation_timestamp_ms = payload.timestamp_ms
    node.status = "online"
    node.version = payload.version
    node.capabilities = payload.capabilities
    await session.commit()


@router.post("/rotate-secret", response_model=NodeSecretRotationResponse)
async def rotate_node_secret(
    payload: NodeSecretRotation,
    response: Response,
    x_node_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> NodeSecretRotationResponse:
    node = await node_from_secret(session, x_node_token, lock=True)
    now = datetime.now(timezone.utc)
    verify_attested_request(
        node,
        node_id=payload.node_id,
        timestamp_ms=payload.timestamp_ms,
        signature=payload.signature,
        message=rotation_payload(payload.node_id, payload.timestamp_ms),
        now=now,
    )
    replacement = opaque_token(48)
    node.secret_hash = token_digest(replacement)
    node.secret_rotated_at = now
    node.last_attested_at = now
    node.last_attestation_timestamp_ms = payload.timestamp_ms
    await session.commit()
    response.headers["Cache-Control"] = "no-store"
    return NodeSecretRotationResponse(node_id=node.id, node_secret=replacement, rotated_at=now)


@router.get("", response_model=list[NodeResponse])
async def list_nodes(session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> list[PairedNode]:
    nodes = list((await session.scalars(select(PairedNode).where(PairedNode.user_id == user.id, PairedNode.revoked_at.is_(None)).order_by(PairedNode.created_at.desc()))).all())
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    for node in nodes:
        if not node.last_seen_at or node.last_seen_at < stale_before:
            node.status = "offline"
    return nodes


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_node(node_id: uuid.UUID, session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> None:
    node = await session.scalar(select(PairedNode).where(PairedNode.id == node_id, PairedNode.user_id == user.id))
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    node.revoked_at = datetime.now(timezone.utc)
    node.status = "revoked"
    await session.commit()
