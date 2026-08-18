import uuid
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


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
    has_password: bool = True
    reauth_methods: list[Literal["password", "google", "passkey"]] = Field(default_factory=list)


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
    current_password: str | None = Field(default=None, min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)
    otp: str | None = Field(default=None, min_length=6, max_length=32)


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


class FactorProofRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class RecoveryCodesResponse(BaseModel):
    recovery_codes: list[str]


class DisableMfaRequest(BaseModel):
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    code: str | None = Field(default=None, min_length=6, max_length=32)


class DeleteAccountRequest(BaseModel):
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    code: str | None = Field(default=None, min_length=6, max_length=32)
    confirmation: str


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    event_type: str
    ip_address: str | None
    user_agent: str | None
    detail: dict | None
    created_at: datetime


class ReauthenticateRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    otp: str | None = Field(default=None, min_length=6, max_length=32)


class PasskeyLoginOptionsRequest(BaseModel):
    email: EmailStr | None = None


class PasskeyOptionsResponse(BaseModel):
    challenge_id: uuid.UUID
    public_key: dict[str, Any]


class PasskeyCredentialRequest(BaseModel):
    challenge_id: uuid.UUID
    credential: dict[str, Any]

    @field_validator("credential")
    @classmethod
    def bounded_credential(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("credential must be finite JSON") from exc
        if len(encoded) > 1024 * 1024:
            raise ValueError("credential may be at most 1 MiB")
        return value


class PasskeyRegistrationRequest(PasskeyCredentialRequest):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class PasskeyRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class PasskeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    transports: list[str]
    device_type: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None
