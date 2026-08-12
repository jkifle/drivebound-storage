import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken as InvalidFernetToken
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.models.auth import AuthSession
from app.models.device import Device
from app.models.user import User

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hash.verify(password, encoded)
    except Exception:
        return False


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes)}
    if session_id is not None:
        claims["sid"] = str(session_id)
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_access_claims(token: str) -> tuple[uuid.UUID, uuid.UUID | None]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})
        return uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"]) if payload.get("sid") else None
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token") from None


def decode_access_token(token: str) -> uuid.UUID:
    return decode_access_claims(token)[0]


async def current_auth(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_db),
) -> tuple[User, AuthSession]:
    token = token or request.cookies.get(settings.auth_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    user_id, session_id = decode_access_claims(token)
    if session_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid")
    now = datetime.now(timezone.utc)
    auth_session = await session.scalar(
        select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
    )
    if auth_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid")
    user = await session.get(User, user_id)
    if user is None or user.disabled_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is unavailable")
    auth_session.last_seen_at = now
    return user, auth_session


async def current_user(auth: tuple[User, AuthSession] = Depends(current_auth)) -> User:
    return auth[0]


async def current_user_or_device(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    x_device_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Authorize browser sessions or a revocable native-device credential."""
    if token or request.cookies.get(settings.auth_cookie_name):
        return await current_user(await current_auth(request, token, session))
    if x_device_token:
        device = await session.scalar(select(Device).where(Device.token_hash == token_digest(x_device_token)))
        if device is not None:
            user = await session.get(User, device.user_id)
            if user is not None and user.disabled_at is None:
                device.last_seen_at = datetime.now(timezone.utc)
                await session.commit()
                return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def opaque_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.mfa_encryption_secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(encoded: str) -> str:
    try:
        return _fernet().decrypt(encoded.encode()).decode()
    except InvalidFernetToken:
        raise HTTPException(status_code=500, detail="MFA secret cannot be decrypted") from None
