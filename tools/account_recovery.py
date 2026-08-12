#!/usr/bin/env python3
"""Host-admin account recovery. Run inside the backend container; never expose as an HTTP endpoint."""
import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.security import hash_password
from app.db.session import async_session_factory
from app.models.auth import AuthSession, AuditEvent
from app.models.user import User


async def recover(email: str, password: str) -> None:
    async with async_session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            raise SystemExit("Account not found")
        user.password_hash = hash_password(password)
        user.email_verified_at = user.email_verified_at or datetime.now(timezone.utc)
        user.disabled_at = None
        user.totp_secret_encrypted = None
        user.totp_enabled_at = None
        await db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=datetime.now(timezone.utc)))
        db.add(AuditEvent(user_id=user.id, event_type="administrator_recovery"))
        await db.commit()
        print("Account recovered; all sessions revoked and two-factor authentication reset.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover a Drivebound account from the trusted host")
    parser.add_argument("email")
    parser.add_argument("--new-password", required=True)
    args = parser.parse_args()
    if len(args.new_password) < 12:
        parser.error("The new password must contain at least 12 characters")
    asyncio.run(recover(args.email, args.new_password))


if __name__ == "__main__":
    main()
