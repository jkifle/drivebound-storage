import base64
import asyncio
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
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import async_session_factory, get_db
from app.models.auth import AuthSession
from app.models.device import Device
from app.models.user import User
from app.services.account_deletion import suppression_subject_is_denied

password_hash = PasswordHash.recommended()
password_kdf_slots = asyncio.Semaphore(4)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)
RECENT_AUTH_HEADER = "X-Drivebound-Reauthentication"


def reject_suppressed_account(user_id: uuid.UUID) -> None:
    """Deny capabilities from an authoritative account-deletion marker."""
    try:
        suppressed = suppression_subject_is_denied(user_id)
    except RuntimeError:
        # Invalid or unavailable suppression evidence is an operational fault,
        # but granting access would risk resurrecting a deleted account.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account authorization is temporarily unavailable",
        ) from None
    if suppressed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is unavailable")


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hash.verify(password, encoded)
    except Exception:
        return False


async def hash_password_async(password: str) -> str:
    return await _bounded_kdf(hash_password, password)


async def verify_password_async(password: str, encoded: str) -> bool:
    return await _bounded_kdf(verify_password, password, encoded)


async def _bounded_kdf(function, *args):
    """Keep capacity reserved until a non-cancellable worker thread exits."""
    async with password_kdf_slots:
        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError as cancellation:
            # A caller may cancel repeatedly. Keep absorbing cancellation until
            # the non-cancellable thread exits so capacity is never released
            # while Argon2 work is still consuming CPU.
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            try:
                work.result()
            except Exception:
                pass
            raise cancellation


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
    reject_suppressed_account(user_id)
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
    last_seen = auth_session.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    if last_seen < now - timedelta(minutes=5):
        # Read-only endpoints usually roll back their dependency session. Keep
        # activity tracking accurate in a short, throttled transaction without
        # turning every request into a write on the route's transaction.
        async with async_session_factory() as activity_session:
            await activity_session.execute(
                update(AuthSession)
                .where(
                    AuthSession.id == auth_session.id,
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                    AuthSession.last_seen_at < now - timedelta(minutes=5),
                    exists().where(User.id == user_id, User.disabled_at.is_(None)),
                )
                .values(last_seen_at=now)
            )
            await activity_session.commit()
    return user, auth_session


async def current_user(auth: tuple[User, AuthSession] = Depends(current_auth)) -> User:
    return auth[0]


def session_has_recent_auth(auth_session: AuthSession, *, now: datetime | None = None) -> bool:
    verified_at = auth_session.reauthenticated_at
    if verified_at is None:
        return False
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - timedelta(minutes=settings.recent_auth_minutes) <= verified_at <= current + timedelta(minutes=1)


async def require_recent_auth(
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> tuple[User, AuthSession]:
    if not session_has_recent_auth(auth[1]):
        raise_recent_auth_required()
    return auth


def raise_recent_auth_required() -> None:
    raise HTTPException(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        detail={
            "code": "recent_auth_required",
            "message": "Please verify your identity to continue.",
        },
        headers={RECENT_AUTH_HEADER: "required"},
    )


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
            reject_suppressed_account(device.user_id)
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
