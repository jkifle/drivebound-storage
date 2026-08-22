#!/usr/bin/env python3
"""Host-admin account recovery. Run inside the backend container; never expose as an HTTP endpoint."""
import argparse
import asyncio
import getpass
import warnings
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select, update

from app.core.security import hash_password
from app.db.session import async_session_factory
from app.models.auth import (
    AccountToken,
    AuthSession,
    AuditEvent,
    ExternalIdentity,
    MfaRecoveryCode,
    PasskeyCredential,
    WebAuthnChallenge,
)
from app.models.account_deletion import AccountDeletionJob
from app.models.device import Device
from app.models.node import NodePairingCode, PairedNode
from app.models.user import User
from app.services.account_deletion import suppression_subject_is_denied


async def recover(email: str, password: str) -> None:
    replacement_hash = hash_password(password)
    async with async_session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email.lower()).with_for_update())
        if user is None:
            raise SystemExit("Account not found")
        try:
            suppressed = suppression_subject_is_denied(user.id)
        except RuntimeError as exc:
            raise SystemExit(
                "Account recovery refused because deletion-suppression evidence cannot be verified"
            ) from exc
        if suppressed:
            raise SystemExit("Account deletion is irreversible and cannot be recovered")
        deletion = await db.scalar(
            select(AccountDeletionJob.id).where(AccountDeletionJob.subject_id == user.id).limit(1)
        )
        if deletion is not None:
            raise SystemExit("Account deletion is irreversible and cannot be recovered")
        now = datetime.now(timezone.utc)
        user.password_hash = replacement_hash
        user.password_enabled = True
        user.password_set_at = now
        user.email_verified_at = user.email_verified_at or now
        user.disabled_at = None
        user.totp_secret_encrypted = None
        user.totp_enabled_at = None
        user.totp_last_used_step = None
        user.pending_totp_secret_encrypted = None
        user.pending_totp_expires_at = None
        user.pending_totp_session_id = None
        await db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=now))
        # Recovery removes every alternate persistence mechanism. External
        # identities must be linked again after the owner signs in locally.
        for model in (
            MfaRecoveryCode,
            AccountToken,
            WebAuthnChallenge,
            PasskeyCredential,
            Device,
            NodePairingCode,
            PairedNode,
            ExternalIdentity,
        ):
            await db.execute(delete(model).where(model.user_id == user.id))
        db.add(AuditEvent(
            user_id=user.id,
            event_type="administrator_recovery",
            detail={"external_identities_removed": True, "all_credentials_revoked": True},
        ))
        await db.commit()
        print("Account recovered; all sessions, devices, passkeys, MFA, tokens, and external identities were revoked.")


def read_password(path: Path | None) -> str:
    if path is not None:
        try:
            # Bound the input before Argon2 sees it. Two extra characters allow
            # a platform line ending after a maximum-length password.
            with path.open("r", encoding="utf-8") as source:
                raw = source.read(1027)
            if len(raw) > 1026:
                raise SystemExit("The new password may contain at most 1024 characters")
            password = raw.rstrip("\r\n")
        except (OSError, UnicodeError) as exc:
            raise SystemExit("Cannot read password file") from exc
    else:
        try:
            # getpass otherwise falls back to an echoed stdin read when no TTY
            # exists, which can publish an administrator password in logs.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("New password: ")
                confirmation = getpass.getpass("Confirm new password: ")
        except (getpass.GetPassWarning, EOFError) as exc:
            raise SystemExit("No private terminal is available; use --password-file with restricted permissions") from exc
        if password != confirmation:
            raise SystemExit("Passwords do not match")
    if len(password) < 12:
        raise SystemExit("The new password must contain at least 12 characters")
    if len(password) > 1024:
        raise SystemExit("The new password may contain at most 1024 characters")
    if "\r" in password or "\n" in password:
        raise SystemExit("The new password cannot contain a line break")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover a Drivebound account from the trusted host")
    parser.add_argument("email")
    parser.add_argument(
        "--password-file",
        type=Path,
        help="Read the new password from a restricted file; otherwise prompt without terminal echo",
    )
    args = parser.parse_args()
    asyncio.run(recover(args.email.strip(), read_password(args.password_file)))


if __name__ == "__main__":
    main()
