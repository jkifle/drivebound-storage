import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    otp: str | None = Field(default=None, min_length=6, max_length=32)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None = None
    onboarding_completed_at: datetime | None = None
    email_verified_at: datetime | None = None
    two_factor_enabled: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class RegistrationResponse(BaseModel):
    message: str
    development_verification_url: str | None = None


class MessageResponse(BaseModel):
    message: str
    development_url: str | None = None


class AuthProvidersResponse(BaseModel):
    google: bool
    google_start_url: str


class EmailRequest(BaseModel):
    email: EmailStr


class TokenRequest(BaseModel):
    token: str = Field(min_length=32, max_length=512)


class ResetPasswordRequest(TokenRequest):
    new_password: str = Field(min_length=12, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class OnboardingUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class SessionResponse(BaseModel):
    id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    last_seen_at: datetime
    expires_at: datetime
    created_at: datetime
    current: bool = False


class TotpSetupResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TotpConfirmRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class DisableMfaRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32)


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str | None = Field(default=None, min_length=6, max_length=32)
    confirmation: str


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    ip_address: str | None
    user_agent: str | None
    detail: dict | None
    created_at: datetime
