import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import current_user, opaque_token, token_digest
from app.db.session import get_db
from app.models.node import NodePairingCode, PairedNode
from app.models.user import User
from app.schemas.node import NodeClaim, NodeHeartbeat, NodeRegistration, NodeResponse, PairingCodeResponse

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post("/pairing-code", response_model=PairingCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_pairing_code(session: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> PairingCodeResponse:
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(8))
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.node_pairing_minutes)
    session.add(NodePairingCode(user_id=user.id, code_hash=token_digest(code), expires_at=expires))
    await session.commit()
    return PairingCodeResponse(code=code, expires_at=expires)


@router.post("/claim", response_model=NodeRegistration, status_code=status.HTTP_201_CREATED)
async def claim_node(payload: NodeClaim, session: AsyncSession = Depends(get_db)) -> NodeRegistration:
    now = datetime.now(timezone.utc)
    pairing = await session.scalar(select(NodePairingCode).where(
        NodePairingCode.code_hash == token_digest(payload.code.upper()),
        NodePairingCode.claimed_at.is_(None), NodePairingCode.expires_at > now,
    ).with_for_update())
    if pairing is None:
        raise HTTPException(status_code=400, detail="Pairing code is invalid or expired")
    if settings.remote_access_enabled and payload.endpoint_url and payload.endpoint_url.scheme != "https":
        raise HTTPException(status_code=400, detail="Remote nodes require an HTTPS endpoint")
    secret = opaque_token(48)
    node = PairedNode(
        user_id=pairing.user_id, name=payload.name, public_key=payload.public_key,
        endpoint_url=str(payload.endpoint_url) if payload.endpoint_url else None,
        secret_hash=token_digest(secret), status="online", last_seen_at=now,
    )
    pairing.claimed_at = now
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return NodeRegistration(node_id=node.id, node_secret=secret)


async def node_from_secret(session: AsyncSession, secret: str | None) -> PairedNode:
    if not secret:
        raise HTTPException(status_code=401, detail="X-Node-Token is required")
    node = await session.scalar(select(PairedNode).where(PairedNode.secret_hash == token_digest(secret), PairedNode.revoked_at.is_(None)))
    if node is None:
        raise HTTPException(status_code=401, detail="Invalid node token")
    return node


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(payload: NodeHeartbeat, x_node_token: str | None = Header(default=None), session: AsyncSession = Depends(get_db)) -> None:
    node = await node_from_secret(session, x_node_token)
    node.last_seen_at = datetime.now(timezone.utc)
    node.status = "online"
    node.version = payload.version
    node.capabilities = payload.capabilities
    await session.commit()


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
