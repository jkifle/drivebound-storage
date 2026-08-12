import asyncio
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import pyotp
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_access_token, current_auth, current_user, decrypt_secret, encrypt_secret,
    hash_password, opaque_token, token_digest, verify_password,
)
from app.db.session import get_db
from app.models.album import Album
from app.models.asset import Asset
from app.models.auth import AccountToken, AuditEvent, AuthSession, ExternalIdentity, MfaRecoveryCode
from app.models.device import Device
from app.models.external_library import ExternalLibrary
from app.models.user import User
from app.schemas.auth import (
    AuditEventResponse, AuthProvidersResponse, ChangePasswordRequest, DeleteAccountRequest, DisableMfaRequest,
    EmailRequest, LoginRequest, MessageResponse, OnboardingUpdate, ProfileUpdate,
    RecoveryCodesResponse, RegisterRequest, RegistrationResponse, ResetPasswordRequest,
    SessionResponse, TokenRequest, TokenResponse, TotpConfirmRequest, TotpSetupResponse,
    UserResponse,
)
from app.services.accounts import (
    create_account_token, deliver_account_email, enforce_rate_limit, record_event, request_context, reset_rate_limit,
)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_STATE_COOKIE = "drivebound_google_state"
GOOGLE_NONCE_COOKIE = "drivebound_google_nonce"


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id, email=user.email, display_name=user.display_name,
        onboarding_completed_at=user.onboarding_completed_at,
        email_verified_at=user.email_verified_at,
        two_factor_enabled=user.totp_enabled_at is not None,
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
        path="/api/v1/auth",
    )


def clear_cookies(response: Response) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/", samesite="strict")
    response.delete_cookie(settings.refresh_cookie_name, path="/api/v1/auth", samesite="strict")


async def issue_session(
    db: AsyncSession, user: User, request: Request, response: Response,
) -> TokenResponse:
    refresh = opaque_token(48)
    ip, agent = request_context(request)
    auth_session = AuthSession(
        user_id=user.id, refresh_token_hash=token_digest(refresh), ip_address=ip,
        user_agent=agent, expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_session_days),
    )
    db.add(auth_session)
    await db.flush()
    access = create_access_token(user.id, auth_session.id)
    set_access_cookie(response, access)
    set_refresh_cookie(response, refresh)
    await record_event(db, request, "login_succeeded", user.id, {"session_id": str(auth_session.id)})
    await db.commit()
    return TokenResponse(access_token=access, user=user_response(user))


async def verify_second_factor(db: AsyncSession, user: User, code: str | None) -> bool:
    if user.totp_enabled_at is None or user.totp_secret_encrypted is None:
        return True
    if not code:
        return False
    normalized = code.replace("-", "").replace(" ", "").upper()
    if normalized.isdigit() and pyotp.TOTP(decrypt_secret(user.totp_secret_encrypted)).verify(normalized, valid_window=1):
        step = int(time.time() // 30)
        if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
            return False
        user.totp_last_used_step = step
        return True
    recovery = await db.scalar(
        select(MfaRecoveryCode).where(
            MfaRecoveryCode.user_id == user.id,
            MfaRecoveryCode.code_hash == token_digest(normalized),
            MfaRecoveryCode.used_at.is_(None),
        )
    )
    if recovery:
        recovery.used_at = datetime.now(timezone.utc)
        return True
    return False


async def valid_account_token(db: AsyncSession, raw: str, purpose: str) -> AccountToken:
    token = await db.scalar(
        select(AccountToken).where(
            AccountToken.token_hash == token_digest(raw), AccountToken.purpose == purpose,
            AccountToken.used_at.is_(None), AccountToken.expires_at > datetime.now(timezone.utc),
        )
    )
    if token is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired")
    return token


def auth_redirect(error: str | None = None) -> RedirectResponse:
    destination = f"{settings.app_url.rstrip('/')}/auth"
    if error:
        destination = f"{destination}?{urlencode({'error': error})}"
    return RedirectResponse(destination, status_code=status.HTTP_302_FOUND)


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
    query = urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_callback_url,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state_token,
        "nonce": nonce,
        "prompt": "select_account",
    })
    response = RedirectResponse(f"{GOOGLE_AUTHORIZE_URL}?{query}", status_code=status.HTTP_302_FOUND)
    cookie_options = {
        "max_age": 600,
        "httponly": True,
        "secure": settings.auth_cookie_secure,
        "samesite": "lax",
        "path": "/api/v1/auth/google",
    }
    response.set_cookie(GOOGLE_STATE_COOKIE, state_token, **cookie_options)
    response.set_cookie(GOOGLE_NONCE_COOKIE, nonce, **cookie_options)
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    redirect = auth_redirect("Google sign-in was cancelled." if error else None)
    redirect.delete_cookie(GOOGLE_STATE_COOKIE, path="/api/v1/auth/google")
    redirect.delete_cookie(GOOGLE_NONCE_COOKIE, path="/api/v1/auth/google")
    if error:
        return redirect
    expected_state = request.cookies.get(GOOGLE_STATE_COOKIE)
    expected_nonce = request.cookies.get(GOOGLE_NONCE_COOKIE)
    if not settings.google_auth_enabled or not code or not state or not secrets.compare_digest(state, expected_state or ""):
        return auth_redirect("Google sign-in could not be verified. Please try again.")
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
        subject = str(claims["sub"])
        email = str(claims["email"]).strip().lower()
        identity = await db.scalar(select(ExternalIdentity).where(
            ExternalIdentity.provider == "google", ExternalIdentity.subject == subject,
        ))
        user = await db.get(User, identity.user_id) if identity else None
        if user is None:
            user = await db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    password_hash=hash_password(opaque_token(48)),
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
            return auth_redirect("This Drivebound account is unavailable.")
        redirect = RedirectResponse(f"{settings.app_url.rstrip('/')}/library", status_code=status.HTTP_302_FOUND)
        await record_event(db, request, "google_login_succeeded", user.id)
        await issue_session(db, user, request, redirect)
        redirect.delete_cookie(GOOGLE_STATE_COOKIE, path="/api/v1/auth/google")
        redirect.delete_cookie(GOOGLE_NONCE_COOKIE, path="/api/v1/auth/google")
        return redirect
    except (httpx.HTTPError, KeyError, ValueError):
        await db.rollback()
        return auth_redirect("Google sign-in failed. Please try again.")


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)) -> RegistrationResponse:
    await enforce_rate_limit(request, f"register:{payload.email}")
    email = str(payload.email).strip().lower()
    if await db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(email=email, password_hash=hash_password(payload.password), display_name=payload.display_name.strip())
    db.add(user)
    await db.flush()
    raw = await create_account_token(db, user, "verify_email", timedelta(hours=24))
    url = f"{settings.app_url.rstrip('/')}/verify-email?token={raw}"
    await record_event(db, request, "account_registered", user.id)
    await db.commit()
    await deliver_account_email(email, "Verify your Drivebound email", f"Verify your account: {url}")
    return RegistrationResponse(
        message="Check your email to verify your account.",
        development_verification_url=url if settings.email_delivery_mode == "console" else None,
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
async def resend_verification(payload: EmailRequest, request: Request, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    await enforce_rate_limit(request, f"verify:{payload.email}")
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()))
    development_url = None
    if user and user.email_verified_at is None:
        raw = await create_account_token(db, user, "verify_email", timedelta(hours=24))
        development_url = f"{settings.app_url.rstrip('/')}/verify-email?token={raw}"
        await db.commit()
        await deliver_account_email(user.email, "Verify your Drivebound email", f"Verify your account: {development_url}")
    return MessageResponse(
        message="If that account needs verification, a new link has been sent.",
        development_url=development_url if settings.email_delivery_mode == "console" else None,
    )


@router.post("/login", response_model=TokenResponse)
async def browser_login(
    payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    await enforce_rate_limit(request, f"login:{payload.email}")
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if user is None or user.disabled_at is not None or not verify_password(payload.password, user.password_hash):
        await record_event(db, request, "login_failed", user.id if user else None)
        await db.commit()
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail="Verify your email before signing in")
    if not await verify_second_factor(db, user, payload.otp):
        await record_event(db, request, "mfa_failed", user.id)
        await db.commit()
        raise HTTPException(status_code=403, detail="Two-factor code required", headers={"X-Drivebound-MFA": "required"})
    await reset_rate_limit(request, f"login:{payload.email}")
    return await issue_session(db, user, request, response)


@router.post("/token", response_model=TokenResponse)
async def token_login(
    request: Request, response: Response, form: OAuth2PasswordRequestForm = Depends(), otp: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    return await browser_login(LoginRequest(email=form.username, password=form.password, otp=otp), request, response, db)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_session(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status_code=401, detail="Refresh session required")
    now = datetime.now(timezone.utc)
    auth_session = await db.scalar(select(AuthSession).where(
        AuthSession.refresh_token_hash == token_digest(raw), AuthSession.revoked_at.is_(None), AuthSession.expires_at > now,
    ))
    if auth_session is None:
        clear_cookies(response)
        raise HTTPException(status_code=401, detail="Refresh session is invalid")
    user = await db.get(User, auth_session.user_id)
    if user is None or user.disabled_at is not None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    replacement = opaque_token(48)
    auth_session.refresh_token_hash = token_digest(replacement)
    auth_session.last_seen_at = now
    access = create_access_token(user.id, auth_session.id)
    set_access_cookie(response, access)
    set_refresh_cookie(response, replacement)
    await db.commit()
    return TokenResponse(access_token=access, user=user_response(user))


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(current_user)) -> UserResponse:
    return user_response(user)


@router.patch("/profile", response_model=UserResponse)
async def update_profile(
    payload: ProfileUpdate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> UserResponse:
    user.display_name = payload.display_name.strip()
    await record_event(db, request, "profile_updated", user.id)
    await db.commit()
    return user_response(user)


@router.post("/onboarding", response_model=UserResponse)
async def complete_onboarding(
    payload: OnboardingUpdate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> UserResponse:
    user.display_name = payload.display_name.strip()
    user.onboarding_completed_at = datetime.now(timezone.utc)
    await record_event(db, request, "onboarding_completed", user.id)
    await db.commit()
    return user_response(user)


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(payload: EmailRequest, request: Request, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    await enforce_rate_limit(request, f"reset:{payload.email}")
    user = await db.scalar(select(User).where(User.email == str(payload.email).lower()))
    development_url = None
    if user and user.disabled_at is None:
        raw = await create_account_token(db, user, "password_reset", timedelta(minutes=30))
        development_url = f"{settings.app_url.rstrip('/')}/reset-password?token={raw}"
        await record_event(db, request, "password_reset_requested", user.id)
        await db.commit()
        await deliver_account_email(user.email, "Reset your Drivebound password", f"Reset your password: {development_url}")
    return MessageResponse(
        message="If an account exists for that email, a reset link has been sent.",
        development_url=development_url if settings.email_delivery_mode == "console" else None,
    )


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(payload: ResetPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    token = await valid_account_token(db, payload.token, "password_reset")
    user = await db.get(User, token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Account no longer exists")
    user.password_hash = hash_password(payload.new_password)
    token.used_at = datetime.now(timezone.utc)
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))
    await record_event(db, request, "password_reset_completed", user.id)
    await db.commit()
    await deliver_account_email(user.email, "Drivebound password changed", "Your Drivebound password was reset. If this was not you, contact your administrator.")
    return MessageResponse(message="Password reset. Sign in with your new password.")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    payload: ChangePasswordRequest, request: Request, db: AsyncSession = Depends(get_db),
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> MessageResponse:
    user, current = auth
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    await db.execute(update(AuthSession).where(AuthSession.user_id == user.id, AuthSession.id != current.id, AuthSession.revoked_at.is_(None)).values(revoked_at=datetime.now(timezone.utc)))
    await record_event(db, request, "password_changed", user.id)
    await db.commit()
    return MessageResponse(message="Password changed. Other sessions were signed out.")


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
    auth: tuple[User, AuthSession] = Depends(current_auth),
) -> None:
    user, _ = auth
    target = await db.scalar(select(AuthSession).where(AuthSession.id == session_id, AuthSession.user_id == user.id))
    if target is None:
        raise HTTPException(status_code=404, detail="Session not found")
    target.revoked_at = datetime.now(timezone.utc)
    await record_event(db, request, "session_revoked", user.id, {"session_id": str(session_id)})
    await db.commit()


@router.post("/mfa/setup", response_model=TotpSetupResponse)
async def setup_mfa(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> TotpSetupResponse:
    secret = pyotp.random_base32()
    user.totp_secret_encrypted = encrypt_secret(secret)
    user.totp_enabled_at = None
    user.totp_last_used_step = None
    await db.commit()
    return TotpSetupResponse(secret=secret, provisioning_uri=pyotp.TOTP(secret).provisioning_uri(user.email, issuer_name="Drivebound"))


@router.post("/mfa/confirm", response_model=RecoveryCodesResponse)
async def confirm_mfa(
    payload: TotpConfirmRequest, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> RecoveryCodesResponse:
    if not user.totp_secret_encrypted or not pyotp.TOTP(decrypt_secret(user.totp_secret_encrypted)).verify(payload.code, valid_window=1):
        raise HTTPException(status_code=400, detail="The authentication code is incorrect")
    await db.execute(update(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id).values(used_at=datetime.now(timezone.utc)))
    codes = [secrets.token_hex(5).upper() for _ in range(8)]
    db.add_all([MfaRecoveryCode(user_id=user.id, code_hash=token_digest(code)) for code in codes])
    user.totp_enabled_at = datetime.now(timezone.utc)
    user.totp_last_used_step = int(time.time() // 30)
    await record_event(db, request, "mfa_enabled", user.id)
    await db.commit()
    return RecoveryCodesResponse(recovery_codes=codes)


@router.delete("/mfa", response_model=MessageResponse)
async def disable_mfa(
    payload: DisableMfaRequest, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> MessageResponse:
    if not verify_password(payload.password, user.password_hash) or not await verify_second_factor(db, user, payload.code):
        raise HTTPException(status_code=400, detail="Password or two-factor code is incorrect")
    user.totp_enabled_at = None
    user.totp_secret_encrypted = None
    user.totp_last_used_step = None
    await db.execute(update(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id).values(used_at=datetime.now(timezone.utc)))
    await record_event(db, request, "mfa_disabled", user.id)
    await db.commit()
    return MessageResponse(message="Two-factor authentication disabled.")


@router.get("/audit", response_model=list[AuditEventResponse])
async def audit_history(
    limit: int = 50, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> list[AuditEventResponse]:
    events = (await db.scalars(select(AuditEvent).where(AuditEvent.user_id == user.id).order_by(AuditEvent.created_at.desc()).limit(min(max(limit, 1), 200)))).all()
    return [AuditEventResponse(id=e.id, event_type=e.event_type, ip_address=e.ip_address, user_agent=e.user_agent, detail=e.detail, created_at=e.created_at) for e in events]


@router.get("/export")
async def export_account(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)) -> dict:
    assets = (await db.scalars(select(Asset).where(Asset.user_id == user.id).order_by(Asset.created_at))).all()
    return {
        "exported_at": datetime.now(timezone.utc),
        "account": user_response(user).model_dump(mode="json"),
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
    payload: DeleteAccountRequest, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
) -> MessageResponse:
    if payload.confirmation != "DELETE MY ACCOUNT" or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Confirmation or password is incorrect")
    if user.totp_enabled_at and not await verify_second_factor(db, user, payload.code):
        raise HTTPException(status_code=400, detail="A valid two-factor code is required")
    await record_event(db, request, "account_deleted", user.id)
    await db.flush()
    await db.delete(user)
    await db.commit()
    return MessageResponse(message="Account metadata deleted. Stored originals remain preserved for administrator recovery.")


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    raw = request.cookies.get(settings.refresh_cookie_name)
    if raw:
        auth_session = await db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == token_digest(raw)))
        if auth_session:
            auth_session.revoked_at = datetime.now(timezone.utc)
            await db.commit()
    clear_cookies(response)
