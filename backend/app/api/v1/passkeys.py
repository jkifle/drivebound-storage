import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.exceptions import WebAuthnException

from app.api.v1.auth import issue_session, require_browser_origin
from app.core.config import settings
from app.core.security import current_auth, require_recent_auth
from app.db.session import get_db
from app.models.auth import AuthSession, ExternalIdentity, PasskeyCredential
from app.models.user import User
from app.schemas.auth import (
    MessageResponse,
    PasskeyCredentialRequest,
    PasskeyLoginOptionsRequest,
    PasskeyOptionsResponse,
    PasskeyRegistrationRequest,
    PasskeyRenameRequest,
    PasskeyResponse,
    TokenResponse,
)
from app.services.accounts import enforce_rate_limit, record_event, reset_rate_limit
from app.services.passkeys import (
    MAX_PASSKEYS_PER_ACCOUNT,
    PASSKEY_AUTHENTICATION,
    PASSKEY_REAUTHENTICATION,
    PASSKEY_REGISTRATION,
    PasskeyCeremonyError,
    authentication_options,
    consume_challenge,
    credential_for_assertion,
    parse_assertion_credential,
    reauthentication_options,
    registration_options,
    verify_assertion,
    verify_registration,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def passkey_response(passkey: PasskeyCredential) -> PasskeyResponse:
    return PasskeyResponse(
        id=passkey.id,
        name=passkey.name,
        transports=list(passkey.transports or []),
        device_type=passkey.device_type,
        backed_up=passkey.backed_up,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )


def user_handle_matches(parsed, user_id: uuid.UUID, *, required: bool) -> bool:
    handle = parsed.response.user_handle
    if handle is None:
        return not required
    return secrets.compare_digest(handle, user_id.bytes)


async def reject_ceremony(
    db: AsyncSession,
    request: Request,
    event_type: str,
    *,
    user_id: uuid.UUID | None = None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> None:
    await record_event(db, request, event_type, user_id)
    await db.commit()
    raise HTTPException(status_code=status_code, detail="Passkey verification failed")


@router.post("/passkeys/login/options", response_model=PasskeyOptionsResponse)
async def passkey_login_options(
    request: Request,
    payload: PasskeyLoginOptionsRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> PasskeyOptionsResponse:
    email = str(payload.email).strip().lower() if payload and payload.email else None
    await enforce_rate_limit(request, f"passkey-email:{email}" if email else None)
    user = None
    if email:
        candidate = await db.scalar(select(User).where(User.email == email))
        if candidate is not None and candidate.disabled_at is None and candidate.email_verified_at is not None:
            user = candidate
    challenge, public_key = await authentication_options(db, user, discoverable=email is None)
    await db.commit()
    return PasskeyOptionsResponse(challenge_id=challenge.id, public_key=public_key)


@router.post("/passkeys/login/verify", response_model=TokenResponse)
async def passkey_login_verify(
    payload: PasskeyCredentialRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    require_browser_origin(request)
    await enforce_rate_limit(request, None)
    user_id: uuid.UUID | None = None
    try:
        parsed = parse_assertion_credential(payload.credential)
    except (WebAuthnException, ValueError, TypeError):
        try:
            await consume_challenge(db, payload.challenge_id, PASSKEY_AUTHENTICATION)
        except PasskeyCeremonyError:
            pass
        await reject_ceremony(
            db,
            request,
            "passkey_login_failed",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise AssertionError("unreachable")

    try:
        candidate = await db.scalar(
            select(PasskeyCredential).where(PasskeyCredential.credential_id == parsed.raw_id)
        )
        if candidate is None:
            await consume_challenge(db, payload.challenge_id, PASSKEY_AUTHENTICATION)
            raise PasskeyCeremonyError("Unknown credential")
        user_id = candidate.user_id
        await enforce_rate_limit(request, f"passkey-account:{candidate.user_id}")
        # Match account-mutation lock order: User -> challenge -> credential.
        # This prevents account deletion from deadlocking or issuing a session
        # from an assertion verified against a concurrently deleted account.
        user = await db.scalar(select(User).where(User.id == candidate.user_id).with_for_update())
        challenge = await consume_challenge(db, payload.challenge_id, PASSKEY_AUTHENTICATION)
        stored = await db.scalar(select(PasskeyCredential).where(
            PasskeyCredential.id == candidate.id,
            PasskeyCredential.credential_id == parsed.raw_id,
            PasskeyCredential.user_id == candidate.user_id,
        ).with_for_update())
        if stored is None:
            raise PasskeyCeremonyError("Unknown credential")
        if challenge.user_id is not None and challenge.user_id != stored.user_id:
            raise PasskeyCeremonyError("Credential owner does not match email-first challenge")
        if challenge.user_id is None and not challenge.discoverable:
            raise PasskeyCeremonyError("Unknown email-first subject")
        if not user_handle_matches(parsed, stored.user_id, required=challenge.discoverable):
            raise PasskeyCeremonyError("Credential user handle is invalid")
        if user is None or user.disabled_at is not None or user.email_verified_at is None:
            raise PasskeyCeremonyError("Account is unavailable")
        verified = await verify_assertion(parsed, stored, challenge)
    except (PasskeyCeremonyError, WebAuthnException, ValueError, TypeError):
        await reject_ceremony(
            db,
            request,
            "passkey_login_failed",
            user_id=user_id,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise AssertionError("unreachable")

    now = datetime.now(timezone.utc)
    stored.sign_count = verified.new_sign_count
    stored.device_type = verified.credential_device_type.value
    stored.backed_up = verified.credential_backed_up
    stored.last_used_at = now
    await record_event(db, request, "passkey_login_succeeded", user.id, {"passkey_id": str(stored.id)})
    result = await issue_session(db, user, request, response, authentication_method="passkey")
    await reset_rate_limit(request, f"passkey-account:{user.id}")
    await reset_rate_limit(request, f"passkey-email:{user.email.lower()}")
    return result


@router.get("/passkeys", response_model=list[PasskeyResponse])
async def list_passkeys(
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> list[PasskeyResponse]:
    credentials = (await db.scalars(
        select(PasskeyCredential).where(PasskeyCredential.user_id == auth[0].id).order_by(PasskeyCredential.created_at)
    )).all()
    return [passkey_response(item) for item in credentials]


@router.post("/passkeys/register/options", response_model=PasskeyOptionsResponse)
async def passkey_registration_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> PasskeyOptionsResponse:
    user = await db.scalar(select(User).where(User.id == auth[0].id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    identity = f"passkey-registration-options:{user.id}"
    await enforce_rate_limit(request, identity)
    try:
        challenge, public_key = await registration_options(db, user, auth[1].id)
    except PasskeyCeremonyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    return PasskeyOptionsResponse(challenge_id=challenge.id, public_key=public_key)


@router.post("/passkeys/register/verify", response_model=PasskeyResponse, status_code=status.HTTP_201_CREATED)
async def passkey_registration_verify(
    payload: PasskeyRegistrationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> PasskeyResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    identity = f"passkey-registration-verify:{user.id}"
    await enforce_rate_limit(request, identity)
    try:
        challenge = await consume_challenge(
            db,
            payload.challenge_id,
            PASSKEY_REGISTRATION,
            user_id=user.id,
            session_id=current.id,
        )
        parsed, verified = await verify_registration(payload.credential, challenge)
    except (PasskeyCeremonyError, WebAuthnException, ValueError, TypeError):
        await reject_ceremony(db, request, "passkey_registration_failed", user_id=user.id)
        raise AssertionError("unreachable")

    existing = await db.scalar(
        select(PasskeyCredential.id).where(PasskeyCredential.credential_id == verified.credential_id)
    )
    if existing is not None:
        await record_event(db, request, "passkey_registration_failed", user.id, {"reason": "duplicate"})
        await db.commit()
        raise HTTPException(status_code=409, detail="Passkey credential is already registered")

    # Serialize the authoritative max-count check across sessions so two
    # simultaneous registrations cannot both pass an options-time count.
    passkey_count = await db.scalar(
        select(func.count()).select_from(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
    )
    if (passkey_count or 0) >= MAX_PASSKEYS_PER_ACCOUNT:
        await record_event(db, request, "passkey_registration_failed", user.id, {"reason": "limit"})
        await db.commit()
        raise HTTPException(status_code=409, detail=f"An account may have at most {MAX_PASSKEYS_PER_ACCOUNT} passkeys")

    credential = PasskeyCredential(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        name=payload.name.strip(),
        transports=[item.value for item in (parsed.response.transports or [])],
        aaguid=verified.aaguid,
        device_type=verified.credential_device_type.value,
        backed_up=verified.credential_backed_up,
    )
    try:
        async with db.begin_nested():
            db.add(credential)
            await db.flush()
    except IntegrityError:
        await record_event(db, request, "passkey_registration_failed", user.id, {"reason": "duplicate"})
        await db.commit()
        raise HTTPException(status_code=409, detail="Passkey credential is already registered") from None
    await db.refresh(credential)
    await record_event(db, request, "passkey_registered", user.id, {"passkey_id": str(credential.id)})
    await db.commit()
    await reset_rate_limit(request, identity)
    return passkey_response(credential)


@router.patch("/passkeys/{passkey_id}", response_model=PasskeyResponse)
async def rename_passkey(
    passkey_id: uuid.UUID,
    payload: PasskeyRenameRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> PasskeyResponse:
    user = await db.scalar(select(User).where(User.id == auth[0].id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    credential = await db.scalar(select(PasskeyCredential).where(
        PasskeyCredential.id == passkey_id,
        PasskeyCredential.user_id == user.id,
    ))
    if credential is None:
        raise HTTPException(status_code=404, detail="Passkey not found")
    credential.name = payload.name.strip()
    await record_event(db, request, "passkey_renamed", user.id, {"passkey_id": str(credential.id)})
    await db.commit()
    return passkey_response(credential)


@router.delete("/passkeys/{passkey_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_passkey(
    passkey_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> None:
    auth_user, _ = auth
    # Serialize every passkey mutation for this account. Without the user-row
    # lock two concurrent deletions could each observe one sibling and remove
    # the final two usable credentials.
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    credential = await db.scalar(select(PasskeyCredential).where(
        PasskeyCredential.id == passkey_id,
        PasskeyCredential.user_id == user.id,
    ).with_for_update())
    if credential is None:
        raise HTTPException(status_code=404, detail="Passkey not found")
    remaining = await db.scalar(
        select(func.count()).select_from(PasskeyCredential).where(
            PasskeyCredential.user_id == user.id,
            PasskeyCredential.id != credential.id,
        )
    )
    if not remaining:
        # Every local account has a password; accounts created by Google keep an
        # ExternalIdentity and may have an intentionally unknowable random hash.
        # Check the latter explicitly rather than treating that hash as a usable
        # recovery method.
        google_identity = await db.scalar(select(ExternalIdentity.id).where(
            ExternalIdentity.user_id == user.id,
            ExternalIdentity.provider == "google",
        ))
        google_available = settings.google_auth_enabled and google_identity is not None
        if not user.password_enabled and not google_available:
            raise HTTPException(status_code=409, detail="Add another sign-in method before deleting this passkey")
    await record_event(db, request, "passkey_deleted", user.id, {"passkey_id": str(credential.id)})
    await db.delete(credential)
    await db.commit()


@router.post("/reauthenticate/passkey/options", response_model=PasskeyOptionsResponse)
async def passkey_reauthentication_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> PasskeyOptionsResponse:
    user = await db.scalar(select(User).where(User.id == auth[0].id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    identity = f"passkey-reauthentication-options:{user.id}"
    await enforce_rate_limit(request, identity)
    try:
        challenge, public_key = await reauthentication_options(db, user, auth[1].id)
    except PasskeyCeremonyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await db.commit()
    return PasskeyOptionsResponse(challenge_id=challenge.id, public_key=public_key)


@router.post("/reauthenticate/passkey/verify", response_model=MessageResponse)
async def passkey_reauthentication_verify(
    payload: PasskeyCredentialRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    identity = f"sensitive-proof:{user.id}"
    await enforce_rate_limit(request, identity)
    try:
        challenge = await consume_challenge(
            db,
            payload.challenge_id,
            PASSKEY_REAUTHENTICATION,
            user_id=user.id,
            session_id=current.id,
        )
        parsed, stored = await credential_for_assertion(db, payload.credential)
        if stored is None or stored.user_id != user.id:
            raise PasskeyCeremonyError("Credential owner mismatch")
        if not user_handle_matches(parsed, user.id, required=False):
            raise PasskeyCeremonyError("Credential user handle is invalid")
        verified = await verify_assertion(parsed, stored, challenge)
    except (PasskeyCeremonyError, WebAuthnException, ValueError, TypeError):
        await reject_ceremony(db, request, "passkey_reauthentication_failed", user_id=user.id)
        raise AssertionError("unreachable")

    now = datetime.now(timezone.utc)
    stored.sign_count = verified.new_sign_count
    stored.device_type = verified.credential_device_type.value
    stored.backed_up = verified.credential_backed_up
    stored.last_used_at = now
    current.reauthenticated_at = now
    await record_event(db, request, "passkey_reauthentication_succeeded", user.id, {"passkey_id": str(stored.id)})
    await db.commit()
    await reset_rate_limit(request, identity)
    return MessageResponse(message="Identity verified.")
