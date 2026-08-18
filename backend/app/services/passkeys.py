"""WebAuthn ceremonies backed by py_webauthn and single-use DB challenges."""

import asyncio
import base64
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    options_to_json_dict,
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import settings
from app.models.auth import PasskeyCredential, WebAuthnChallenge
from app.models.user import User

PASSKEY_REGISTRATION = "passkey_registration"
PASSKEY_AUTHENTICATION = "passkey_authentication"
PASSKEY_REAUTHENTICATION = "passkey_reauthentication"
MAX_PASSKEYS_PER_ACCOUNT = 10
MAX_CHALLENGE_CLEANUP = 100


class PasskeyCeremonyError(ValueError):
    """A public-safe WebAuthn ceremony failure."""


def passkey_options(document) -> dict:
    return options_to_json_dict(document)


def _credential_id_matches(parsed) -> bool:
    canonical_raw_id = base64.urlsafe_b64encode(parsed.raw_id).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(str(parsed.id), canonical_raw_id)


def parse_assertion_credential(credential_document: dict):
    parsed = parse_authentication_credential_json(credential_document)
    if not _credential_id_matches(parsed):
        raise PasskeyCeremonyError("Credential id and rawId do not match")
    return parsed


async def cleanup_challenges(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete a bounded batch so public option requests cannot grow the table forever."""
    current = now or datetime.now(timezone.utc)
    ids = list((await session.scalars(
        select(WebAuthnChallenge.id).where(
            or_(
                WebAuthnChallenge.expires_at < current - timedelta(hours=1),
                (
                    WebAuthnChallenge.used_at.is_not(None)
                    & (WebAuthnChallenge.created_at < current - timedelta(days=1))
                ),
            )
        ).order_by(WebAuthnChallenge.created_at).limit(MAX_CHALLENGE_CLEANUP)
    )).all())
    if ids:
        await session.execute(delete(WebAuthnChallenge).where(WebAuthnChallenge.id.in_(ids)))
    return len(ids)


async def create_challenge(
    session: AsyncSession,
    *,
    purpose: str,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    discoverable: bool = False,
) -> WebAuthnChallenge:
    if purpose == PASSKEY_AUTHENTICATION:
        if session_id is not None:
            raise ValueError("Login challenges cannot be session-bound")
    elif purpose in {PASSKEY_REGISTRATION, PASSKEY_REAUTHENTICATION}:
        if user_id is None or session_id is None:
            raise ValueError("Registration and reauthentication challenges require user and session bindings")
        discoverable = False
    else:
        raise ValueError("Unsupported WebAuthn challenge purpose")

    now = datetime.now(timezone.utc)
    await cleanup_challenges(session, now=now)
    if session_id is not None:
        # Only the most recently issued ceremony of a given protected type stays
        # usable for this session. Public login challenges remain independent.
        await session.execute(
            update(WebAuthnChallenge).where(
                WebAuthnChallenge.session_id == session_id,
                WebAuthnChallenge.purpose == purpose,
                WebAuthnChallenge.used_at.is_(None),
            ).values(used_at=now)
        )
    challenge = WebAuthnChallenge(
        user_id=user_id,
        session_id=session_id,
        purpose=purpose,
        challenge=secrets.token_bytes(32),
        discoverable=discoverable,
        expires_at=now + timedelta(minutes=settings.webauthn_challenge_minutes),
    )
    session.add(challenge)
    await session.flush()
    return challenge


async def consume_challenge(
    session: AsyncSession,
    challenge_id: uuid.UUID,
    purpose: str,
    *,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> WebAuthnChallenge:
    challenge = await session.scalar(
        select(WebAuthnChallenge).where(
            WebAuthnChallenge.id == challenge_id,
            WebAuthnChallenge.purpose == purpose,
        ).with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        challenge is None
        or challenge.used_at is not None
        or challenge.expires_at <= now
        or (user_id is not None and challenge.user_id != user_id)
        or (session_id is not None and challenge.session_id != session_id)
    ):
        raise PasskeyCeremonyError("Passkey challenge is invalid or expired")
    challenge.used_at = now
    await session.flush()
    return challenge


async def registration_options(
    session: AsyncSession,
    user: User,
    session_id: uuid.UUID,
) -> tuple[WebAuthnChallenge, dict]:
    credentials = list((await session.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user.id).order_by(PasskeyCredential.created_at)
    )).all())
    if len(credentials) >= MAX_PASSKEYS_PER_ACCOUNT:
        raise PasskeyCeremonyError(f"An account may have at most {MAX_PASSKEYS_PER_ACCOUNT} passkeys")
    challenge = await create_challenge(
        session,
        purpose=PASSKEY_REGISTRATION,
        user_id=user.id,
        session_id=session_id,
    )
    options = generate_registration_options(
        rp_id=settings.webauthn_effective_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.display_name or user.email,
        challenge=challenge.challenge,
        timeout=settings.webauthn_challenge_minutes * 60 * 1000,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
    )
    return challenge, passkey_options(options)


async def authentication_options(
    session: AsyncSession,
    user: User | None,
    *,
    discoverable: bool,
) -> tuple[WebAuthnChallenge, dict]:
    challenge = await create_challenge(
        session,
        purpose=PASSKEY_AUTHENTICATION,
        user_id=user.id if user is not None and not discoverable else None,
        session_id=None,
        discoverable=discoverable,
    )
    options = generate_authentication_options(
        rp_id=settings.webauthn_effective_rp_id,
        challenge=challenge.challenge,
        timeout=settings.webauthn_challenge_minutes * 60 * 1000,
        # Registration requires resident credentials. Omitting allowCredentials
        # for both email-first and username-less flows keeps their public option
        # shapes indistinguishable; the email-first user binding is enforced
        # only after a signed assertion is verified.
        allow_credentials=None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return challenge, passkey_options(options)


async def reauthentication_options(
    session: AsyncSession,
    user: User,
    session_id: uuid.UUID,
) -> tuple[WebAuthnChallenge, dict]:
    credentials = list((await session.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user.id).order_by(PasskeyCredential.created_at)
    )).all())
    if not credentials:
        raise PasskeyCeremonyError("This account does not have a passkey")
    challenge = await create_challenge(
        session,
        purpose=PASSKEY_REAUTHENTICATION,
        user_id=user.id,
        session_id=session_id,
    )
    options = generate_authentication_options(
        rp_id=settings.webauthn_effective_rp_id,
        challenge=challenge.challenge,
        timeout=settings.webauthn_challenge_minutes * 60 * 1000,
        allow_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return challenge, passkey_options(options)


async def verify_registration(credential: dict, challenge: WebAuthnChallenge):
    parsed = parse_registration_credential_json(credential)
    if not _credential_id_matches(parsed):
        raise PasskeyCeremonyError("Credential id and rawId do not match")
    verified = await asyncio.to_thread(
        verify_registration_response,
        credential=parsed,
        expected_challenge=challenge.challenge,
        expected_rp_id=settings.webauthn_effective_rp_id,
        expected_origin=settings.webauthn_origin_list,
        require_user_verification=True,
    )
    return parsed, verified


async def credential_for_assertion(
    session: AsyncSession,
    credential_document: dict,
) -> tuple[object, PasskeyCredential | None]:
    parsed = parse_assertion_credential(credential_document)
    stored = await session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == parsed.raw_id).with_for_update()
    )
    return parsed, stored


async def verify_assertion(parsed, stored: PasskeyCredential, challenge: WebAuthnChallenge):
    return await asyncio.to_thread(
        verify_authentication_response,
        credential=parsed,
        expected_challenge=challenge.challenge,
        expected_rp_id=settings.webauthn_effective_rp_id,
        expected_origin=settings.webauthn_origin_list,
        credential_public_key=stored.public_key,
        credential_current_sign_count=stored.sign_count,
        require_user_verification=True,
    )
