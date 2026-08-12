import asyncio
import hashlib
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import opaque_token, token_digest
from app.models.auth import AccountToken, AuditEvent
from app.models.user import User


def request_context(request: Request) -> tuple[str | None, str | None]:
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",", 1)[0].strip() if forwarded else (request.client.host if request.client else None)
    return ip, request.headers.get("user-agent", "")[:512] or None


async def record_event(session: AsyncSession, request: Request, event_type: str, user_id=None, detail=None) -> None:
    ip, agent = request_context(request)
    session.add(AuditEvent(user_id=user_id, event_type=event_type, ip_address=ip, user_agent=agent, detail=detail))


async def enforce_rate_limit(request: Request, identity: str) -> None:
    ip, _ = request_context(request)
    fingerprint = hashlib.sha256(f"{identity.lower()}:{ip}".encode()).hexdigest()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        key = f"auth-rate:{fingerprint}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.auth_rate_limit_window_seconds)
        if count > settings.auth_rate_limit_attempts:
            raise HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")
    except HTTPException:
        raise
    except Exception:
        # Authentication remains available during a Redis outage; the outage is visible through health monitoring.
        return
    finally:
        await redis.aclose()


async def reset_rate_limit(request: Request, identity: str) -> None:
    ip, _ = request_context(request)
    fingerprint = hashlib.sha256(f"{identity.lower()}:{ip}".encode()).hexdigest()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(f"auth-rate:{fingerprint}")
    except Exception:
        return
    finally:
        await redis.aclose()


async def create_account_token(
    session: AsyncSession, user: User, purpose: str, lifetime: timedelta
) -> str:
    token = opaque_token(40)
    session.add(AccountToken(
        user_id=user.id,
        purpose=purpose,
        token_hash=token_digest(token),
        expires_at=datetime.now(timezone.utc) + lifetime,
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
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        smtp.send_message(message)


async def deliver_account_email(to: str, subject: str, body: str) -> None:
    if settings.email_delivery_mode == "smtp":
        await asyncio.to_thread(_send_email, to, subject, body)
