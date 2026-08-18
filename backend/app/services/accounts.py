import asyncio
import hashlib
import logging
import smtplib
import ssl
from functools import lru_cache
from ipaddress import ip_address
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import opaque_token, token_digest
from app.models.auth import AccountToken, AuditEvent
from app.models.user import User

logger = logging.getLogger(__name__)


def request_context(request: Request) -> tuple[str | None, str | None]:
    peer = request.client.host if request.client else None
    client_ip = peer
    if peer:
        try:
            trusted_peer = any(ip_address(peer) in network for network in settings.trusted_proxy_networks)
        except ValueError:
            trusted_peer = False
        if trusted_peer:
            forwarded = request.headers.get("x-forwarded-for")
            candidate = forwarded.split(",", 1)[0].strip() if forwarded else ""
            try:
                client_ip = str(ip_address(candidate)) if candidate else peer
            except ValueError:
                client_ip = peer
    return client_ip, request.headers.get("user-agent", "")[:512] or None


async def record_event(session: AsyncSession, request: Request, event_type: str, user_id=None, detail=None) -> None:
    ip, agent = request_context(request)
    session.add(AuditEvent(user_id=user_id, event_type=event_type, ip_address=ip, user_agent=agent, detail=detail))


async def enforce_rate_limit(request: Request, identity: str | None) -> None:
    ip, _ = request_context(request)
    pepper = settings.jwt_secret
    ip_fingerprint = hashlib.sha256(f"{pepper}:ip:{ip}".encode()).hexdigest()
    if identity is None:
        # Username-less ceremonies have no safe global identity yet. Bound the
        # source IP without using a shared constant that could lock out every
        # account. The larger budget accommodates ordinary shared NATs.
        fingerprints = [ip_fingerprint]
    else:
        normalized_identity = identity.lower()
        fingerprints = [
            hashlib.sha256(f"{pepper}:pair:{normalized_identity}:{ip}".encode()).hexdigest(),
            hashlib.sha256(f"{pepper}:identity:{normalized_identity}".encode()).hexdigest(),
            ip_fingerprint,
        ]
    redis = auth_rate_redis(settings.redis_url)
    try:
        counts = await redis.eval(
            """
            local result = {}
            for index, key in ipairs(KEYS) do
                local count = redis.call('INCR', key)
                local ttl = redis.call('TTL', key)
                if ttl < 0 then
                    redis.call('EXPIRE', key, ARGV[1])
                end
                result[index] = count
            end
            return result
            """,
            len(fingerprints),
            *(f"auth-rate:{fingerprint}" for fingerprint in fingerprints),
            settings.auth_rate_limit_window_seconds,
        )
        numeric_counts = [int(value) for value in counts]
        exceeded = (
            numeric_counts[0] > settings.auth_rate_limit_attempts * 10
            if identity is None
            else (
                numeric_counts[0] > settings.auth_rate_limit_attempts
                or numeric_counts[1] > settings.auth_rate_limit_attempts
                or numeric_counts[2] > settings.auth_rate_limit_attempts * 10
            )
        )
        if exceeded:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait and try again.",
                headers={"Retry-After": str(settings.auth_rate_limit_window_seconds)},
            )
    except HTTPException:
        raise
    except Exception:
        if settings.remote_access_enabled or settings.production_like:
            raise HTTPException(
                status_code=503,
                detail="Authentication protection is temporarily unavailable. Please try again.",
                headers={"Retry-After": "30"},
            ) from None
        # A loopback-only development install remains usable without Redis.
        return


async def reset_rate_limit(request: Request, identity: str | None) -> None:
    if identity is None:
        # Never erase the shared-NAT/IP abuse budget after a single success.
        return
    ip, _ = request_context(request)
    normalized_identity = identity.lower()
    pepper = settings.jwt_secret
    pair = hashlib.sha256(f"{pepper}:pair:{normalized_identity}:{ip}".encode()).hexdigest()
    account = hashlib.sha256(f"{pepper}:identity:{normalized_identity}".encode()).hexdigest()
    redis = auth_rate_redis(settings.redis_url)
    try:
        await redis.delete(f"auth-rate:{pair}", f"auth-rate:{account}")
    except Exception:
        return


@lru_cache(maxsize=4)
def auth_rate_redis(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        health_check_interval=30,
    )


async def create_account_token(
    session: AsyncSession,
    user: User,
    purpose: str,
    lifetime: timedelta,
    *,
    auth_session_id=None,
) -> str:
    # A newer account-action link supersedes every older outstanding link for
    # the same purpose. This prevents an older reset link from being replayed
    # after a subsequent request or successful reset.
    now = datetime.now(timezone.utc)
    await session.execute(
        update(AccountToken)
        .where(
            AccountToken.user_id == user.id,
            AccountToken.purpose == purpose,
            AccountToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    token = opaque_token(40)
    session.add(AccountToken(
        user_id=user.id,
        session_id=auth_session_id,
        purpose=purpose,
        token_hash=token_digest(token),
        expires_at=now + lifetime,
    ))
    return token


def _send_email(to: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is required when EMAIL_DELIVERY_MODE=smtp")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            # smtplib's implicit default is not a sufficiently explicit
            # production contract. Require the platform trust store, hostname
            # verification, and certificate validation.
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        smtp.send_message(message)


async def deliver_account_email(to: str, subject: str, body: str) -> None:
    if settings.email_delivery_mode == "smtp":
        await asyncio.to_thread(_send_email, to, subject, body)


async def deliver_account_email_safely(to: str, subject: str, body: str) -> None:
    """Deliver after the public response without turning provider state into an oracle."""
    try:
        await deliver_account_email(to, subject, body)
    except Exception:
        # Never log the body: it may contain a reset or verification token.
        logger.exception("Account email delivery failed", extra={"email_subject": subject})
