import asyncio
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

import httpx
import pyotp
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token, current_auth, current_user, decrypt_secret, encrypt_secret,
    hash_password, hash_password_async, opaque_token, raise_recent_auth_required, require_recent_auth,
    session_has_recent_auth, token_digest, verify_password_async,
)
from app.db.session import get_db
from app.models.album import Album
from app.models.asset import Asset
from app.models.auth import AccountToken, AuditEvent, AuthSession, ExternalIdentity, MfaRecoveryCode, PasskeyCredential
from app.models.device import Device
from app.models.external_library import ExternalLibrary
from app.models.user import User
from app.schemas.auth import (
    AuditEventResponse, AuthProvidersResponse, ChangePasswordRequest, DeleteAccountRequest, DisableMfaRequest,
    EmailRequest, FactorProofRequest, LoginRequest, MessageResponse, OnboardingUpdate, ProfileUpdate,
    RecoveryCodesResponse, RegisterRequest, RegistrationResponse, ResetPasswordRequest,
    ReauthenticateRequest, SessionResponse, TokenRequest, TokenResponse, TotpConfirmRequest, TotpSetupResponse,
    UserResponse,
)
from app.services.accounts import (
    create_account_token, deliver_account_email_safely, enforce_rate_limit, record_event, request_context,
    reset_rate_limit,
)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DUMMY_PASSWORD_HASH = hash_password("drivebound-dummy-password-proof")


def google_state_cookie_name() -> str:
    return "__Host-drivebound_google_state" if settings.auth_cookie_secure else "drivebound_google_state"


def google_nonce_cookie_name() -> str:
    return "__Host-drivebound_google_nonce" if settings.auth_cookie_secure else "drivebound_google_nonce"


def google_mfa_cookie_name() -> str:
    return "__Host-drivebound_google_mfa" if settings.auth_cookie_secure else "drivebound_google_mfa"


def google_authorization_response(
    state_token: str,
    nonce: str,
    *,
    reauthenticate: bool = False,
) -> RedirectResponse:
    query = urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_token,
        "nonce": nonce,
        "prompt": "login" if reauthenticate else "select_account",
        **({"max_age": "0"} if reauthenticate else {}),
    })
    response = RedirectResponse(f"{GOOGLE_AUTHORIZE_URL}?{query}", status_code=status.HTTP_302_FOUND)
    cookie_options = {
        "max_age": 600,
        "httponly": True,
        "secure": settings.auth_cookie_secure,
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(google_state_cookie_name(), state_token, **cookie_options)
    response.set_cookie(google_nonce_cookie_name(), nonce, **cookie_options)
    return response


def google_mfa_redirect(transaction_token: str, mode: str) -> RedirectResponse:
    response = RedirectResponse(
        f"{settings.app_url.rstrip('/')}/auth/google-mfa?{urlencode({'mode': mode})}",
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        google_mfa_cookie_name(),
        transaction_token,
        max_age=300,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


def clear_google_mfa_cookie(response: Response) -> Response:
    response.delete_cookie(
        google_mfa_cookie_name(),
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


def google_mfa_problem(status_code: int, code: str, message: str) -> JSONResponse:
    return clear_google_mfa_cookie(JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    ))


async def reauthentication_methods(db: AsyncSession, user: User) -> list[str]:
    methods: list[str] = []
    if user.password_enabled is not False:
        methods.append("password")
    if settings.google_auth_enabled and await db.scalar(select(ExternalIdentity.id).where(
        ExternalIdentity.user_id == user.id,
        ExternalIdentity.provider == "google",
    ).limit(1)) is not None:
        methods.append("google")
    if await db.scalar(select(PasskeyCredential.id).where(
        PasskeyCredential.user_id == user.id,
    ).limit(1)) is not None:
        methods.append("passkey")
    return methods


async def user_response(db: AsyncSession, user: User) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, display_name=user.display_name,
        onboarding_completed_at=user.onboarding_completed_at,
        email_verified_at=user.email_verified_at,
        two_factor_enabled=user.totp_enabled_at is not None,
        has_password=user.password_enabled is not False,
        reauth_methods=await reauthentication_methods(db, user),
    )


def set_access_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name, token, max_age=settings.access_token_minutes * 60,
        httponly=True, secure=settings.auth_cookie_secure, samesite="strict", path="/",
    )


def set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.refresh_cookie_name, token, max_age=settings.refresh_session_days * 86400,
        httponly=True, secure=settings.auth_cookie_secure, samesite="strict",
        path="/",
    )


def clear_cookies(response: Response) -> None:
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        settings.refresh_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="strict",
    )


async def issue_session(
    db: AsyncSession, user: User, request: Request, response: Response,
    authentication_method: str = "password",
    *,
    set_browser_cookies: bool = True,
    establish_recent_auth: bool = True,
) -> TokenResponse:
    refresh = opaque_token(48)
    ip, agent = request_context(request)
    now = datetime.now(timezone.utc)
    auth_session = AuthSession(
        user_id=user.id, refresh_token_hash=token_digest(refresh), ip_address=ip,
        user_agent=agent,
        expires_at=(
            now + timedelta(days=settings.refresh_session_days)
            if set_browser_cookies
            else now + timedelta(minutes=settings.access_token_minutes)
        ),
        reauthenticated_at=now if establish_recent_auth else None,
    )
    db.add(auth_session)
    await db.flush()
    access = create_access_token(user.id, auth_session.id)
    if set_browser_cookies:
        set_access_cookie(response, access)
        set_refresh_cookie(response, refresh)
    await record_event(db, request, "login_succeeded", user.id, {
        "session_id": str(auth_session.id),
        "authentication_method": authentication_method,
        "recent_auth_established": establish_recent_auth,
    })
    await db.commit()
    return TokenResponse(access_token=access, user=await user_response(db, user))


async def verify_second_factor(db: AsyncSession, user: User, code: str | None) -> bool:
    if user.totp_enabled_at is None and user.totp_secret_encrypted is None:
        return True
    if user.totp_enabled_at is None or user.totp_secret_encrypted is None:
        return False
    if not code:
        return False
    locked_user = await db.scalar(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )
    if locked_user is None:
        return False
    user = locked_user
    if user.totp_enabled_at is None or user.totp_secret_encrypted is None:
        return False
    normalized = code.replace("-", "").replace(" ", "").upper()
    if normalized.isdigit():
        totp = pyotp.TOTP(decrypt_secret(user.totp_secret_encrypted))
        matched_step = matching_totp_step(totp, normalized)
        if matched_step is not None:
            if user.totp_last_used_step is not None and matched_step <= user.totp_last_used_step:
                return False
            user.totp_last_used_step = matched_step
            return True
    recovery = await db.scalar(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == token_digest(normalized),
            MfaRecoveryCode.used_at.is_(None),
        ).with_for_update()
    )
    if recovery:
        if recovery.used_at is not None:
            return False
        recovery.used_at = datetime.now(timezone.utc)
        return True
    return False


def matching_totp_step(totp: pyotp.TOTP, code: str) -> int | None:
    current_step = int(time.time() // totp.interval)
    return next(
        (
            step for step in range(current_step - 1, current_step + 2)
            if secrets.compare_digest(totp.at(step * totp.interval), code)
        ),
        None,
    )


async def valid_account_token(db: AsyncSession, raw: str, purpose: str) -> AccountToken:
    digest = token_digest(raw)
    candidate = await db.execute(select(AccountToken.user_id).where(
        AccountToken.token_hash == digest,
        AccountToken.purpose == purpose,
    ))
    user_id = candidate.scalar_one_or_none()
    if user_id is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired")
    # Account-token issuance also locks User first. Keeping a single lock order
    # avoids issuance-vs-consumption deadlocks and makes supersession atomic.
    if await db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired")
    token = await db.scalar(
        select(AccountToken).where(
            AccountToken.token_hash == digest, AccountToken.purpose == purpose,
            AccountToken.used_at.is_(None), AccountToken.expires_at > datetime.now(timezone.utc),
        ).with_for_update()
    )
    if token is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired")
    return token


def auth_redirect(error: str | None = None) -> RedirectResponse:
    destination = f"{settings.app_url.rstrip('/')}/auth"
    if error:
        destination = f"{destination}?{urlencode({'error': error})}"
    return RedirectResponse(destination, status_code=status.HTTP_302_FOUND)


def security_redirect(*, reauthenticated: str | None = None, error: str | None = None) -> RedirectResponse:
    query: dict[str, str] = {"tab": "security"}
    if reauthenticated:
        query["reauthenticated"] = reauthenticated
    if error:
        query["reauth_error"] = error
    return RedirectResponse(
        f"{settings.app_url.rstrip('/')}/profile?{urlencode(query)}",
        status_code=status.HTTP_302_FOUND,
    )


class GoogleReauthenticationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require_fresh_google_authentication(claims: dict, *, now: datetime | None = None) -> None:
    try:
        authenticated_at = int(claims["auth_time"])
    except (KeyError, TypeError, ValueError):
        raise GoogleReauthenticationError("google_reauth_failed") from None
    current_timestamp = int((now or datetime.now(timezone.utc)).timestamp())
    if authenticated_at < current_timestamp - 300 or authenticated_at > current_timestamp + 60:
        raise GoogleReauthenticationError("google_reauth_failed")


def clear_google_cookies(response: Response) -> Response:
    response.delete_cookie(
        google_state_cookie_name(),
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        google_nonce_cookie_name(),
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


def verify_google_id_token(raw_token: str) -> dict:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(raw_token, google_requests.Request(), settings.google_client_id)


@router.get("/providers", response_model=AuthProvidersResponse)
async def auth_providers() -> AuthProvidersResponse:
    return AuthProvidersResponse(
        google=settings.google_auth_enabled,
        google_start_url=f"{settings.api_url.rstrip('/')}/api/v1/auth/google/start",
    )


@router.get("/google/start")
async def google_start() -> RedirectResponse:
    if not settings.google_auth_enabled:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    state_token = opaque_token(32)
    nonce = opaque_token(32)
    return google_authorization_response(state_token, nonce)


@router.get("/reauthenticate/google/start")
async def google_reauthentication_start(
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> RedirectResponse:
    if not settings.google_auth_enabled:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    user, current = auth
    identity = await db.scalar(select(ExternalIdentity.id).where(
        ExternalIdentity.user_id == user.id,
        ExternalIdentity.provider == "google",
    ))
    if identity is None:
        return security_redirect(error="google_not_linked")
    state_token = f"reauth.{opaque_token(32)}"
    nonce = opaque_token(32)
    db.add(AccountToken(
        user_id=user.id,
        session_id=current.id,
        purpose="google_reauth",
        token_hash=token_digest(state_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    await record_event(db, request, "google_reauthentication_started", user.id)
    await db.commit()
    return google_authorization_response(state_token, nonce, reauthenticate=True)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    is_reauthentication = bool(state and state.startswith("reauth."))
    redirect = clear_google_cookies(
        security_redirect(error="google_reauth_cancelled")
        if error and is_reauthentication
        else auth_redirect("Google sign-in was cancelled." if error else None)
    )
    if error:
        return redirect
    expected_state = request.cookies.get(google_state_cookie_name())
    expected_nonce = request.cookies.get(google_nonce_cookie_name())
    if not settings.google_auth_enabled or not code or not state or not secrets.compare_digest(state, expected_state or ""):
        return clear_google_cookies(
            security_redirect(error="google_reauth_failed")
            if is_reauthentication
            else auth_redirect("Google sign-in could not be verified. Please try again.")
        )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(GOOGLE_TOKEN_URL, data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": settings.google_callback_url,
            })
            token_response.raise_for_status()
            raw_id_token = token_response.json()["id_token"]
        claims = await asyncio.to_thread(verify_google_id_token, raw_id_token)
        if claims.get("nonce") != expected_nonce or not claims.get("email_verified"):
            raise ValueError("Google identity is not verified")
        if is_reauthentication:
            require_fresh_google_authentication(claims)
        subject = str(claims["sub"])
        email = str(claims["email"]).strip().lower()
        reauth_candidate = await db.scalar(select(AccountToken).where(
            AccountToken.token_hash == token_digest(state),
            AccountToken.purpose == "google_reauth",
        ))
        if state.startswith("reauth.") and reauth_candidate is None:
            raise GoogleReauthenticationError("google_reauth_expired")
        if reauth_candidate is not None:
            user = await db.scalar(
                select(User).where(User.id == reauth_candidate.user_id).with_for_update()
            )
            current = await db.scalar(select(AuthSession).where(
                AuthSession.id == reauth_candidate.session_id,
                AuthSession.user_id == reauth_candidate.user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > datetime.now(timezone.utc),
            ).with_for_update())
            reauth_token = await db.scalar(select(AccountToken).where(
                AccountToken.id == reauth_candidate.id,
                AccountToken.token_hash == token_digest(state),
                AccountToken.purpose == "google_reauth",
            ).with_for_update())
            if reauth_token is None:
                raise GoogleReauthenticationError("google_reauth_expired")
            if reauth_token.used_at is not None or reauth_token.expires_at <= datetime.now(timezone.utc):
                raise GoogleReauthenticationError("google_reauth_expired")
            identity = await db.scalar(select(ExternalIdentity).where(
                ExternalIdentity.provider == "google",
                ExternalIdentity.subject == subject,
                ExternalIdentity.user_id == reauth_token.user_id,
            ))
            if identity is None:
                raise GoogleReauthenticationError("google_reauth_failed")
            if current is None:
                raise GoogleReauthenticationError("google_reauth_expired")
            if user is None or user.disabled_at is not None:
                raise GoogleReauthenticationError("google_reauth_failed")
            now = datetime.now(timezone.utc)
            if user.totp_enabled_at is not None:
                # Google proves the federated identity, but it does not attest
                # that Drivebound's local second factor was used. Defer the
                # recent-auth grant until a one-attempt local MFA transaction.
                reauth_token.used_at = now
                mfa_token = await create_account_token(
                    db,
                    user,
                    "google_mfa_reauth",
                    timedelta(minutes=5),
                    auth_session_id=current.id,
                )
                await record_event(db, request, "google_mfa_required", user.id, {"mode": "reauthenticate"})
                await db.commit()
                return clear_google_cookies(google_mfa_redirect(mfa_token, "reauthenticate"))
            current.reauthenticated_at = now
            reauth_token.used_at = now
            await record_event(db, request, "google_reauthentication_succeeded", reauth_token.user_id)
            await db.commit()
            redirect = security_redirect(reauthenticated="google")
            return clear_google_cookies(redirect)
        identity = await db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.provider == "google", ExternalIdentity.subject == subject,
        ))
        user = await db.scalar(
            select(User).where(User.id == identity.user_id).with_for_update()
        ) if identity else None
        if user is None:
            user = await db.scalar(select(User).where(User.email == email).with_for_update())
            if user is None:
                user = User(
                    email=email,
                    password_hash=await hash_password_async(opaque_token(48)),
                    password_enabled=False,
                    display_name=str(claims.get("name") or email.split("@", 1)[0])[:120],
                    email_verified_at=datetime.now(timezone.utc),
                )
                db.add(user)
                await db.flush()
            elif user.email_verified_at is None:
                user.email_verified_at = datetime.now(timezone.utc)
            db.add(ExternalIdentity(user_id=user.id, provider="google", subject=subject, email_at_link=email))
            await db.flush()
        if user.disabled_at is not None:
            return clear_google_cookies(auth_redirect("This Drivebound account is unavailable."))
        if user.totp_enabled_at is not None:
            mfa_token = await create_account_token(
                db,
                user,
                "google_mfa_login",
                timedelta(minutes=5),
            )
            await record_event(db, request, "google_mfa_required", user.id, {"mode": "login"})
            await db.commit()
            return clear_google_cookies(google_mfa_redirect(mfa_token, "login"))
        redirect = RedirectResponse(f"{settings.app_url.rstrip('/')}/library", status_code=status.HTTP_302_FOUND)
        await record_event(db, request, "google_login_succeeded", user.id)
        await issue_session(
            db,
            user,
            request,
            redirect,
            authentication_method="google",
            establish_recent_auth=False,
        )
        return clear_google_cookies(redirect)
    except GoogleReauthenticationError as exc:
        await db.rollback()
        return clear_google_cookies(security_redirect(error=exc.code))
    except (httpx.HTTPError, KeyError, ValueError):
        await db.rollback()
        return clear_google_cookies(
            security_redirect(error="google_reauth_failed")
            if is_reauthentication
            else auth_redirect("Google sign-in failed. Please try again.")
        )


async def consume_google_mfa_transaction(
    db: AsyncSession,
    request: Request,
    purpose: str,
) -> tuple[AccountToken, User, AuthSession | None] | None:
    raw = request.cookies.get(google_mfa_cookie_name())
    if not raw:
        return None
    now = datetime.now(timezone.utc)
    candidate = await db.execute(select(AccountToken.user_id, AccountToken.session_id).where(
        AccountToken.token_hash == token_digest(raw),
        AccountToken.purpose == purpose,
    ))
    binding = candidate.one_or_none()
    if binding is None:
        return None
    user_id, session_id = binding
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    auth_session = None
    if session_id is not None:
        auth_session = await db.scalar(select(AuthSession).where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        ).with_for_update())
    token = await db.scalar(select(AccountToken).where(
        AccountToken.token_hash == token_digest(raw),
        AccountToken.purpose == purpose,
    ).with_for_update())
    if token is None or token.used_at is not None:
        return None
    expiry = token.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= now:
        token.used_at = now
        await db.commit()
        return None
    token.used_at = now
    if (
        user is None
        or user.disabled_at is not None
        or user.totp_enabled_at is None
        or (session_id is not None and auth_session is None)
    ):
        await db.commit()
        return None
    return token, user, auth_session


@router.post("/google/mfa/login/complete", response_model=TokenResponse)
async def complete_google_mfa_login(
    payload: FactorProofRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse | Response:
    require_browser_origin(request)
    transaction = await consume_google_mfa_transaction(db, request, "google_mfa_login")
    if transaction is None:
        return google_mfa_problem(
            status.HTTP_400_BAD_REQUEST,
            "google_mfa_transaction_invalid",
            "This Google verification has expired or was already used. Start again.",
        )
    token, user, _ = transaction
    identity = f"sensitive-proof:{user.id}"
    await enforce_rate_limit(request, identity)
    if not await verify_second_factor(db, user, payload.code):
        await record_event(db, request, "google_mfa_failed", user.id, {"mode": "login"})
        await db.commit()
        return google_mfa_problem(
            status.HTTP_401_UNAUTHORIZED,
            "google_mfa_code_invalid",
            "The authentication code is incorrect. Start Google sign-in again.",
        )
    await record_event(db, request, "google_mfa_succeeded", user.id, {"mode": "login"})
    clear_google_mfa_cookie(response)
    result = await issue_session(db, user, request, response, authentication_method="google+totp")
    await reset_rate_limit(request, identity)
    return result


@router.post("/google/mfa/reauthenticate/complete", response_model=MessageResponse)
async def complete_google_mfa_reauthentication(
    payload: FactorProofRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse | Response:
    require_browser_origin(request)
    transaction = await consume_google_mfa_transaction(db, request, "google_mfa_reauth")
    if transaction is None:
        return google_mfa_problem(
            status.HTTP_400_BAD_REQUEST,
            "google_mfa_transaction_invalid",
            "This Google verification has expired or was already used. Start again.",
        )
    token, user, current = transaction
    identity = f"sensitive-proof:{user.id}"
    await enforce_rate_limit(request, identity)
    if not await verify_second_factor(db, user, payload.code):
        await record_event(db, request, "google_mfa_failed", user.id, {"mode": "reauthenticate"})
        await db.commit()
        return google_mfa_problem(
            status.HTTP_401_UNAUTHORIZED,
            "google_mfa_code_invalid",
            "The authentication code is incorrect. Start Google verification again.",
        )
    if current is None:
        await db.commit()
        return google_mfa_problem(
            status.HTTP_400_BAD_REQUEST,
            "google_mfa_transaction_invalid",
            "The original session is no longer available. Start again.",
        )
    current.reauthenticated_at = datetime.now(timezone.utc)
    await record_event(db, request, "google_mfa_succeeded", user.id, {"mode": "reauthenticate"})
    await db.commit()
    await reset_rate_limit(request, identity)
    clear_google_mfa_cookie(response)
    return MessageResponse(message="Identity verified.")


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> RegistrationResponse:
    await enforce_rate_limit(request, f"register:{payload.email}")
    email = str(payload.email).strip().lower()
    if await db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(
        email=email,
        password_hash=await hash_password_async(payload.password),
        password_enabled=True,
        password_set_at=datetime.now(timezone.utc),
        display_name=payload.display_name.strip(),
    )
    db.add(user)
    await db.flush()
    raw = await create_account_token(db, user, "verify_email", timedelta(hours=24))
    url = f"{settings.app_url.rstrip('/')}/verify-email#token={raw}"
    await record_event(db, request, "account_registered", user.id)
    await db.commit()
    background_tasks.add_task(
        deliver_account_email_safely,
        email,
        "Verify your Drivebound email",
        f"Verify your account: {url}",
    )
    return RegistrationResponse(
        message="Check your email to verify your account.",
        development_verification_url=url if settings.development_email_urls_enabled else None,
    )


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(payload: TokenRequest, request: Request, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    token = await valid_account_token(db, payload.token, "verify_email")
    user = await db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Account no longer exists")
    user.email_verified_at = datetime.now(timezone.utc)
    token.used_at = datetime.now(timezone.utc)
    await record_event(db, request, "email_verified", user.id)
    await db.commit()
    return MessageResponse(message="Email verified. You can now sign in.")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    payload: EmailRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await enforce_rate_limit(request, f"verify:{payload.email}")
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()).with_for_update())
    development_url = None
    if user and user.email_verified_at is None:
        raw = await create_account_token(db, user, "verify_email", timedelta(hours=24))
        development_url = f"{settings.app_url.rstrip('/')}/verify-email#token={raw}"
        await db.commit()
        background_tasks.add_task(
            deliver_account_email_safely,
            user.email,
            "Verify your Drivebound email",
            f"Verify your account: {development_url}",
        )
    return MessageResponse(
        message="If that account needs verification, a new link has been sent.",
        development_url=development_url if settings.development_email_urls_enabled else None,
    )


def require_browser_origin(request: Request) -> None:
    if not (settings.remote_access_enabled or settings.production_like):
        return
    origin = request.headers.get("Origin", "").rstrip("/")
    parsed = urlsplit(settings.app_url)
    allowed = {item.rstrip("/") for item in settings.cors_origin_list}
    allowed.add(f"{parsed.scheme}://{parsed.netloc}".rstrip("/"))
    if not origin or origin not in allowed:
        raise HTTPException(status_code=403, detail="Request origin is not allowed")


async def password_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession,
    *,
    set_browser_cookies: bool,
) -> TokenResponse:
    await enforce_rate_limit(request, f"login:{payload.email}")
    user = await db.scalar(
        select(User).where(User.email == str(payload.email).lower()).with_for_update()
    )
    password_valid = await verify_password_async(
        payload.password,
        user.password_hash if user is not None else DUMMY_PASSWORD_HASH,
    )
    if user is None or user.disabled_at is not None or not password_valid:
        await record_event(db, request, "login_failed", user.id if user else None)
        await db.commit()
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="Verify your email before signing in")
    if not await verify_second_factor(db, user, payload.otp):
        await record_event(db, request, "mfa_failed", user.id)
        await db.commit()
        raise HTTPException(status_code=403, detail="Two-factor code required", headers={"X-Drivebound-MFA": "required"})
    if not user.password_enabled:
        # A successful password proof resolves conservative legacy inference for
        # accounts that linked Google before credential-state tracking existed.
        user.password_enabled = True
        user.password_set_at = datetime.now(timezone.utc)
    result = await issue_session(
        db,
        user,
        request,
        response,
        set_browser_cookies=set_browser_cookies,
    )
    await reset_rate_limit(request, f"login:{payload.email}")
    return result


@router.post("/login", response_model=TokenResponse)
async def browser_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    require_browser_origin(request)
    return await password_login(payload, request, response, db, set_browser_cookies=True)


@router.post("/token", response_model=TokenResponse)
async def token_login(
    request: Request, response: Response, form: OAuth2PasswordRequestForm = Depends(), otp: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    return await password_login(
        LoginRequest(email=form.username, password=form.password, otp=otp),
        request,
        response,
        db,
        set_browser_cookies=False,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse | Response:
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status_code=401, detail="Refresh session required")
    now = datetime.now(timezone.utc)
    auth_session = await db.scalar(select(AuthSession).where(
        AuthSession.refresh_token_hash == token_digest(raw), AuthSession.revoked_at.is_(None), AuthSession.expires_at > now,
    ).with_for_update())
    if auth_session is None:
        problem = JSONResponse(status_code=401, content={"detail": "Refresh session is invalid"})
        clear_cookies(problem)
        return problem
    user = await db.get(User, auth_session.user_id)
    if user is None or user.disabled_at is not None:
        problem = JSONResponse(status_code=401, content={"detail": "Account is unavailable"})
        clear_cookies(problem)
        return problem
    replacement = opaque_token(48)
    auth_session.refresh_token_hash = token_digest(replacement)
    auth_session.last_seen_at = now
    access = create_access_token(user.id, auth_session.id)
    set_access_cookie(response, access)
    set_refresh_cookie(response, replacement)
    await db.commit()
    return TokenResponse(access_token=access, user=await user_response(db, user))


@router.get("/me", response_model=UserResponse)
async def me(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> UserResponse:
    return await user_response(db, user)


@router.patch("/profile", response_model=UserResponse)
async def update_profile(
    payload: ProfileUpdate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> UserResponse:
    user.display_name = payload.display_name.strip()
    await record_event(db, request, "profile_updated", user.id)
    await db.commit()
    return await user_response(db, user)


@router.post("/onboarding", response_model=UserResponse)
async def complete_onboarding(
    payload: OnboardingUpdate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> UserResponse:
    user.display_name = payload.display_name.strip()
    user.onboarding_completed_at = datetime.now(timezone.utc)
    await record_event(db, request, "onboarding_completed", user.id)
    await db.commit()
    return await user_response(db, user)


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    payload: EmailRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await enforce_rate_limit(request, f"reset:{payload.email}")
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()).with_for_update())
    development_url = None
    if user and user.disabled_at is None:
        raw = await create_account_token(db, user, "password_reset", timedelta(minutes=30))
        development_url = f"{settings.app_url.rstrip('/')}/reset-password#token={raw}"
        await record_event(db, request, "password_reset_requested", user.id)
        await db.commit()
        background_tasks.add_task(
            deliver_account_email_safely,
            user.email,
            "Reset your Drivebound password",
            f"Reset your password: {development_url}",
        )
    return MessageResponse(
        message="If an account exists for that email, a reset link has been sent.",
        development_url=development_url if settings.development_email_urls_enabled else None,
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    token = await valid_account_token(db, payload.token, "password_reset")
    user = await db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Account no longer exists")
    user.password_hash = await hash_password_async(payload.new_password)
    user.password_enabled = True
    user.password_set_at = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    token.used_at = now
    await db.execute(update(AccountToken).where(
        AccountToken.user_id == user.id,
        AccountToken.purpose == "password_reset",
        AccountToken.used_at.is_(None),
    ).values(used_at=now))
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))
    await record_event(db, request, "password_reset_completed", user.id)
    await db.commit()
    background_tasks.add_task(
        deliver_account_email_safely,
        user.email,
        "Drivebound password changed",
        "Your Drivebound password was reset. If this was not you, contact your administrator.",
    )
    return MessageResponse(message="Password reset. Sign in with your new password.")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: ChangePasswordRequest, request: Request, db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    was_enabled = user.password_enabled
    if user.password_enabled:
        proof_identity = f"sensitive-proof:{user.id}"
        await enforce_rate_limit(request, proof_identity)
        if (
            not payload.current_password
            or not await verify_password_async(payload.current_password, user.password_hash)
            or not await verify_second_factor(db, user, payload.otp)
        ):
            await record_event(db, request, "password_change_failed", user.id)
            await db.commit()
            raise HTTPException(status_code=400, detail="Current password or two-factor code is incorrect")
    elif not session_has_recent_auth(current):
        raise_recent_auth_required()
    user.password_hash = await hash_password_async(payload.new_password)
    user.password_enabled = True
    user.password_set_at = datetime.now(timezone.utc)
    current.reauthenticated_at = datetime.now(timezone.utc)
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.id != current.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))
    await record_event(db, request, "password_changed", user.id)
    await db.commit()
    if was_enabled:
        await reset_rate_limit(request, proof_identity)
    action = "changed" if was_enabled else "set"
    return MessageResponse(message=f"Password {action}. Other sessions were signed out.")


@router.post("/reauthenticate/password", response_model=MessageResponse)
async def reauthenticate_password(
    payload: ReauthenticateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    if not user.password_enabled:
        raise HTTPException(status_code=409, detail="This account does not have a password. Use Google or a passkey.")
    identity = f"sensitive-proof:{user.id}"
    await enforce_rate_limit(request, identity)
    if not await verify_password_async(payload.password, user.password_hash) or not await verify_second_factor(db, user, payload.otp):
        await record_event(db, request, "reauthentication_failed", user.id, {"method": "password"})
        await db.commit()
        raise HTTPException(status_code=401, detail="Password or two-factor code is incorrect")
    current.reauthenticated_at = datetime.now(timezone.utc)
    await record_event(db, request, "reauthentication_succeeded", user.id, {"method": "password"})
    await db.commit()
    await reset_rate_limit(request, identity)
    return MessageResponse(message="Identity verified.")


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    auth: tuple[User, AuthSession] = Depends(current_auth), db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    user, current = auth
    sessions = (await db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > datetime.now(timezone.utc),
    ).order_by(AuthSession.last_seen_at.desc()))).all()
    return [SessionResponse(
        id=item.id, ip_address=item.ip_address, user_agent=item.user_agent,
        last_seen_at=item.last_seen_at, expires_at=item.expires_at, created_at=item.created_at,
        current=item.id == current.id,
    ) for item in sessions]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> None:
    user, _ = auth
    target = await db.scalar(select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id))
    if target is None:
        raise HTTPException(status_code=404, detail="Session not found")
    target.revoked_at = datetime.now(timezone.utc)
    await record_event(db, request, "session_revoked", user.id, {"session_id": str(session_id)})
    await db.commit()


@router.post("/mfa/setup", response_model=TotpSetupResponse)
async def setup_mfa(
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> TotpSetupResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    secret = pyotp.random_base32()
    user.pending_totp_secret_encrypted = encrypt_secret(secret)
    user.pending_totp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    user.pending_totp_session_id = current.id
    await db.commit()
    return TotpSetupResponse(secret=secret, provisioning_uri=pyotp.TOTP(secret).provisioning_uri(user.email, issuer_name="Drivebound"))


@router.post("/mfa/confirm", response_model=RecoveryCodesResponse)
async def confirm_mfa(
    payload: TotpConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> RecoveryCodesResponse:
    auth_user, current = auth
    user = await db.scalar(
        select(User).where(User.id == auth_user.id).with_for_update().execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    identity = f"sensitive-proof:{user.id}"
    await enforce_rate_limit(request, identity)
    now = datetime.now(timezone.utc)
    pending_expiry = user.pending_totp_expires_at
    if pending_expiry is not None and pending_expiry.tzinfo is None:
        pending_expiry = pending_expiry.replace(tzinfo=timezone.utc)
    matched_step: int | None = None
    if user.pending_totp_secret_encrypted:
        pending_totp = pyotp.TOTP(decrypt_secret(user.pending_totp_secret_encrypted))
        matched_step = matching_totp_step(pending_totp, payload.code)
    if (
        not user.pending_totp_secret_encrypted
        or pending_expiry is None
        or pending_expiry <= now
        or user.pending_totp_session_id != current.id
        or matched_step is None
    ):
        await record_event(db, request, "mfa_confirmation_failed", user.id)
        await db.commit()
        raise HTTPException(status_code=400, detail="The authentication code is incorrect")
    user.totp_secret_encrypted = user.pending_totp_secret_encrypted
    user.pending_totp_secret_encrypted = None
    user.pending_totp_expires_at = None
    user.pending_totp_session_id = None
    await db.execute(update(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id).values(used_at=datetime.now(timezone.utc)))
    codes = [secrets.token_hex(10).upper() for _ in range(8)]
    db.add_all([MfaRecoveryCode(user_id=user.id, code_hash=token_digest(code)) for code in codes])
    user.totp_enabled_at = datetime.now(timezone.utc)
    user.totp_last_used_step = matched_step
    current.reauthenticated_at = now
    await record_event(db, request, "mfa_enabled", user.id)
    await db.commit()
    await reset_rate_limit(request, identity)
    return RecoveryCodesResponse(recovery_codes=codes)


@router.delete("/mfa", response_model=MessageResponse)
async def disable_mfa(
    payload: DisableMfaRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    if not user.password_enabled and not session_has_recent_auth(current):
        raise_recent_auth_required()
    if user.password_enabled:
        proof_identity = f"sensitive-proof:{user.id}"
        await enforce_rate_limit(request, proof_identity)
        authorized = (
            bool(payload.password)
            and await verify_password_async(payload.password or "", user.password_hash)
            and await verify_second_factor(db, user, payload.code)
        )
    else:
        authorized = True
    if not authorized:
        await record_event(db, request, "mfa_disable_failed", user.id)
        await db.commit()
        raise HTTPException(status_code=400, detail="Password or two-factor code is incorrect")
    user.totp_enabled_at = None
    user.totp_secret_encrypted = None
    user.totp_last_used_step = None
    user.pending_totp_secret_encrypted = None
    user.pending_totp_expires_at = None
    user.pending_totp_session_id = None
    current.reauthenticated_at = datetime.now(timezone.utc)
    await db.execute(update(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id).values(used_at=datetime.now(timezone.utc)))
    await record_event(db, request, "mfa_disabled", user.id)
    await db.commit()
    if user.password_enabled:
        await reset_rate_limit(request, proof_identity)
    return MessageResponse(message="Two-factor authentication disabled.")


@router.get("/audit", response_model=list[AuditEventResponse])
async def audit_history(
    limit: int = 50, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> list[AuditEventResponse]:
    events = (await db.scalars(select(AuditEvent).where(AuditEvent.user_id == user.id).order_by(AuditEvent.created_at.desc()).limit(min(max(limit, 1), 200)))).all()
    return [AuditEventResponse(id=e.id, event_type=e.event_type, ip_address=e.ip_address, user_agent=e.user_agent, detail=e.detail, created_at=e.created_at) for e in events]


@router.get("/export")
async def export_account(
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(require_recent_auth),
) -> dict:
    user, _ = auth
    assets = (await db.scalars(select(Asset).where(Asset.user_id == user.id).order_by(Asset.created_at))).all()
    return {
        "exported_at": datetime.now(timezone.utc),
        "account": (await user_response(db, user)).model_dump(mode="json"),
        "counts": {
            "assets": len(assets),
            "albums": await db.scalar(select(func.count()).select_from(Album).where(Album.user_id == user.id)),
            "libraries": await db.scalar(select(func.count()).select_from(ExternalLibrary).where(ExternalLibrary.user_id == user.id)),
            "devices": await db.scalar(select(func.count()).select_from(Device).where(Device.user_id == user.id)),
        },
        "assets": [{
            "id": str(a.id), "filename": a.original_filename, "checksum": a.checksum,
            "file_size": a.file_size, "mime_type": a.mime_type,
            "taken_at": a.taken_at, "created_at": a.created_at,
            "original_url": f"/api/v1/assets/{a.id}/original",
        } for a in assets],
    }


@router.delete("/account", response_model=MessageResponse)
async def delete_account(
    payload: DeleteAccountRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    auth_user, current = auth
    user = await db.scalar(select(User).where(User.id == auth_user.id).with_for_update())
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    if payload.confirmation != "DELETE MY ACCOUNT":
        raise HTTPException(status_code=400, detail="Confirmation is incorrect")
    proof_identity = f"sensitive-proof:{user.id}"
    if user.password_enabled:
        await enforce_rate_limit(request, proof_identity)
    if user.password_enabled:
        if not payload.password or not await verify_password_async(payload.password, user.password_hash):
            await record_event(db, request, "account_delete_failed", user.id)
            await db.commit()
            raise HTTPException(status_code=400, detail="Password is incorrect")
    elif not session_has_recent_auth(current):
        raise_recent_auth_required()
    if user.password_enabled and user.totp_enabled_at and not await verify_second_factor(db, user, payload.code):
        await record_event(db, request, "account_delete_failed", user.id)
        await db.commit()
        raise HTTPException(status_code=400, detail="A valid two-factor code is required")
    if user.password_enabled:
        await reset_rate_limit(request, proof_identity)
    await record_event(db, request, "account_deleted", user.id)
    await db.flush()
    await db.delete(user)
    await db.commit()
    clear_cookies(response)
    return MessageResponse(
        message=(
            "Account and media-encryption keys deleted. Any retained orphaned media bytes are inaccessible; "
            "physical storage cleanup is an administrator responsibility."
        )
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    raw = request.cookies.get(settings.refresh_cookie_name)
    if raw:
        auth_session = await db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == token_digest(raw)))
        if auth_session:
            auth_session.revoked_at = datetime.now(timezone.utc)
            await db.commit()
    clear_cookies(response)
