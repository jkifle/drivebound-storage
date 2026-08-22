import asyncio
import base64
import hashlib
import json
import ssl
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Request, Response
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from webauthn.helpers import parse_authentication_credential_json
from webauthn.helpers.exceptions import WebAuthnException

from app.api.v1.auth import (
    GoogleReauthenticationError,
    complete_google_mfa_login,
    consume_google_mfa_transaction,
    google_authorization_response,
    issue_session,
    reauthentication_methods,
    require_fresh_google_authentication,
    set_access_cookie,
    set_refresh_cookie,
)
from app.core.config import settings
from app.core import security
from app.main import app, runtime_configuration_errors
from app.models.auth import AccountToken, AuthSession, MfaRecoveryCode, PasskeyCredential, WebAuthnChallenge
from app.models.user import User
from app.schemas.auth import FactorProofRequest, UserResponse
from app.services import accounts
from app.services.passkeys import (
    PASSKEY_AUTHENTICATION,
    PASSKEY_REGISTRATION,
    PasskeyCeremonyError,
    authentication_options,
    consume_challenge,
    create_challenge,
    parse_assertion_credential,
    verify_assertion,
    verify_registration,
)


def run(coro):
    return asyncio.run(coro)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def client_data(kind: str, challenge: bytes, origin: str) -> bytes:
    return json.dumps(
        {"type": kind, "challenge": b64url(challenge), "origin": origin, "crossOrigin": False},
        separators=(",", ":"),
    ).encode()


def registration_document(
    private_key: ec.EllipticCurvePrivateKey,
    credential_id: bytes,
    challenge: bytes,
    *,
    rp_id: str = "localhost",
    origin: str = "http://localhost:3000",
    user_verified: bool = True,
) -> dict:
    numbers = private_key.public_key().public_numbers()
    cose_key = cbor2.dumps({
        1: 2,
        3: -7,
        -1: 1,
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    })
    flags = 0x01 | 0x40 | (0x04 if user_verified else 0)
    auth_data = (
        hashlib.sha256(rp_id.encode()).digest()
        + bytes([flags])
        + struct.pack(">I", 0)
        + (b"\0" * 16)
        + struct.pack(">H", len(credential_id))
        + credential_id
        + cose_key
    )
    encoded_id = b64url(credential_id)
    return {
        "id": encoded_id,
        "rawId": encoded_id,
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "clientDataJSON": b64url(client_data("webauthn.create", challenge, origin)),
            "attestationObject": b64url(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})),
            "transports": ["internal"],
        },
    }


def assertion_document(
    private_key: ec.EllipticCurvePrivateKey,
    credential_id: bytes,
    challenge: bytes,
    user_id: uuid.UUID,
    *,
    sign_count: int,
    rp_id: str = "localhost",
    origin: str = "http://localhost:3000",
    user_verified: bool = True,
) -> dict:
    flags = 0x01 | (0x04 if user_verified else 0)
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", sign_count)
    client_json = client_data("webauthn.get", challenge, origin)
    signature = private_key.sign(auth_data + hashlib.sha256(client_json).digest(), ec.ECDSA(hashes.SHA256()))
    encoded_id = b64url(credential_id)
    return {
        "id": encoded_id,
        "rawId": encoded_id,
        "type": "public-key",
        "authenticatorAttachment": "platform",
        "clientExtensionResults": {},
        "response": {
            "authenticatorData": b64url(auth_data),
            "clientDataJSON": b64url(client_json),
            "signature": b64url(signature),
            "userHandle": b64url(user_id.bytes),
        },
    }


@pytest.fixture
def webauthn_settings():
    old = (settings.webauthn_rp_id, settings.webauthn_origins, settings.app_url, settings.api_url)
    settings.webauthn_rp_id = "localhost"
    settings.webauthn_origins = "http://localhost:3000"
    settings.app_url = "http://localhost:3000"
    settings.api_url = "http://localhost:8000"
    try:
        yield
    finally:
        settings.webauthn_rp_id, settings.webauthn_origins, settings.app_url, settings.api_url = old


def test_real_webauthn_registration_and_counter_replay_checks(webauthn_settings) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    credential_id = b"credential-id-123"
    user_id = uuid.uuid4()
    challenge = WebAuthnChallenge(
        purpose=PASSKEY_REGISTRATION,
        challenge=b"r" * 32,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    parsed, verified = run(verify_registration(
        registration_document(private_key, credential_id, challenge.challenge),
        challenge,
    ))
    assert parsed.raw_id == credential_id
    assert verified.credential_id == credential_id
    assert verified.user_verified is True

    stored = PasskeyCredential(
        user_id=user_id,
        credential_id=credential_id,
        public_key=verified.credential_public_key,
        sign_count=0,
        name="Laptop",
        transports=["internal"],
        device_type="single_device",
        backed_up=False,
    )
    auth_challenge = WebAuthnChallenge(
        purpose=PASSKEY_AUTHENTICATION,
        challenge=b"a" * 32,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    parsed_assertion = parse_authentication_credential_json(
        assertion_document(private_key, credential_id, auth_challenge.challenge, user_id, sign_count=1)
    )
    result = run(verify_assertion(parsed_assertion, stored, auth_challenge))
    assert result.new_sign_count == 1
    stored.sign_count = 1
    with pytest.raises(WebAuthnException):
        run(verify_assertion(parsed_assertion, stored, auth_challenge))


@pytest.mark.parametrize(
    ("rp_id", "origin", "uv"),
    [
        ("evil.example", "http://localhost:3000", True),
        ("localhost", "https://evil.example", True),
        ("localhost", "http://localhost:3000", False),
    ],
)
def test_webauthn_rejects_wrong_rp_origin_and_missing_uv(webauthn_settings, rp_id, origin, uv) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    challenge = WebAuthnChallenge(
        purpose=PASSKEY_REGISTRATION,
        challenge=b"c" * 32,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with pytest.raises(WebAuthnException):
        run(verify_registration(
            registration_document(
                private_key,
                b"credential",
                challenge.challenge,
                rp_id=rp_id,
                origin=origin,
                user_verified=uv,
            ),
            challenge,
        ))


def test_webauthn_rejects_mismatched_id_and_raw_id(webauthn_settings) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_id = uuid.uuid4()
    document = assertion_document(
        private_key,
        b"credential",
        b"z" * 32,
        user_id,
        sign_count=1,
    )
    document["id"] = b64url(b"different")
    with pytest.raises(PasskeyCeremonyError, match="id and rawId"):
        parse_assertion_credential(document)


class ResultList:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class OptionSession:
    def __init__(self):
        self.added = []

    async def scalars(self, _statement):
        return ResultList([])

    async def execute(self, _statement):
        return None

    def add(self, item):
        if item.id is None:
            item.id = uuid.uuid4()
        self.added.append(item)

    async def flush(self):
        return None


def test_public_passkey_options_do_not_enumerate_accounts(webauthn_settings) -> None:
    user = User(id=uuid.uuid4(), email="person@example.com", password_hash="unused")
    unknown_challenge, unknown = run(authentication_options(OptionSession(), None, discoverable=False))
    known_challenge, known = run(authentication_options(OptionSession(), user, discoverable=False))
    discoverable_challenge, discoverable = run(authentication_options(OptionSession(), None, discoverable=True))
    assert unknown_challenge.user_id is None
    assert known_challenge.user_id == user.id
    assert discoverable_challenge.discoverable is True
    for document in (unknown, known, discoverable):
        document.pop("challenge")
        assert not document.get("allowCredentials")
    assert unknown == known == discoverable


class ChallengeSession:
    def __init__(self, challenge):
        self.challenge = challenge
        self.flushes = 0

    async def scalar(self, _statement):
        return self.challenge

    async def flush(self):
        self.flushes += 1


def test_challenge_is_purpose_bound_expiring_and_one_attempt() -> None:
    challenge = WebAuthnChallenge(
        id=uuid.uuid4(),
        purpose=PASSKEY_AUTHENTICATION,
        challenge=b"x" * 32,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    session = ChallengeSession(challenge)
    assert run(consume_challenge(session, challenge.id, PASSKEY_AUTHENTICATION)) is challenge
    with pytest.raises(PasskeyCeremonyError):
        run(consume_challenge(session, challenge.id, PASSKEY_AUTHENTICATION))

    with pytest.raises(ValueError, match="Login challenges"):
        run(create_challenge(
            OptionSession(),
            purpose=PASSKEY_AUTHENTICATION,
            user_id=None,
            session_id=uuid.uuid4(),
        ))
    with pytest.raises(ValueError, match="require user and session"):
        run(create_challenge(
            OptionSession(),
            purpose=PASSKEY_REGISTRATION,
            user_id=uuid.uuid4(),
            session_id=None,
        ))


def test_factor_proof_accepts_long_recovery_code_and_user_contract_is_bounded() -> None:
    assert FactorProofRequest(code="A" * 20).code == "A" * 20
    response = UserResponse(id=uuid.uuid4(), email="person@example.com", reauth_methods=["google", "passkey"])
    assert response.reauth_methods == ["google", "passkey"]
    with pytest.raises(Exception):
        UserResponse(id=uuid.uuid4(), email="person@example.com", reauth_methods=["unknown"])


class BindingResult:
    def __init__(self, binding):
        self.binding = binding

    def one_or_none(self):
        return self.binding


class MfaTransactionSession:
    def __init__(self, token, user, auth_session=None):
        self.token = token
        self.user = user
        self.auth_session = auth_session
        self.scalar_calls = 0
        self.commits = 0

    async def execute(self, _statement):
        return BindingResult((self.token.user_id, self.token.session_id))

    async def scalar(self, _statement):
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.user
        if self.auth_session is not None and self.scalar_calls == 2:
            return self.auth_session
        return self.token

    async def commit(self):
        self.commits += 1


def request_with_cookie(name: str, value: str) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"cookie", f"{name}={value}".encode())],
        "client": ("127.0.0.1", 1234),
    })


def test_google_mfa_transaction_is_session_bound_expiring_and_one_attempt(monkeypatch) -> None:
    raw = "one-time-google-mfa"
    user = User(
        id=uuid.uuid4(),
        email="person@example.com",
        password_hash="unused",
        totp_secret_encrypted="encrypted",
        totp_enabled_at=datetime.now(timezone.utc),
    )
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash="f" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    token = AccountToken(
        user_id=user.id,
        session_id=auth_session.id,
        purpose="google_mfa_reauth",
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    session = MfaTransactionSession(token, user, auth_session)
    request = request_with_cookie("drivebound_google_mfa", raw)
    transaction = run(consume_google_mfa_transaction(session, request, "google_mfa_reauth"))
    assert transaction == (token, user, auth_session)
    assert token.used_at is not None

    second = MfaTransactionSession(token, user, auth_session)
    assert run(consume_google_mfa_transaction(second, request, "google_mfa_reauth")) is None


def test_google_reauthentication_forces_fresh_login_and_validates_auth_time() -> None:
    response = google_authorization_response("state", "nonce", reauthenticate=True)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]
    now = datetime.now(timezone.utc)
    require_fresh_google_authentication({"auth_time": int(now.timestamp())}, now=now)
    for claims in (
        {},
        {"auth_time": int((now - timedelta(minutes=6)).timestamp())},
        {"auth_time": int((now + timedelta(minutes=2)).timestamp())},
    ):
        with pytest.raises(GoogleReauthenticationError):
            require_fresh_google_authentication(claims, now=now)


class SessionIssueDb:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def scalar(self, _statement):
        return None

    async def commit(self):
        return None


def test_bearer_token_session_issuance_can_never_set_browser_cookies() -> None:
    user = User(id=uuid.uuid4(), email="person@example.com", password_hash="unused", password_enabled=True)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
    response = Response()
    db = SessionIssueDb()
    before = datetime.now(timezone.utc)
    run(issue_session(db, user, request, response, set_browser_cookies=False))
    assert "set-cookie" not in response.headers
    auth_session = next(item for item in db.added if isinstance(item, AuthSession))
    assert auth_session.expires_at <= before + timedelta(minutes=settings.access_token_minutes, seconds=2)


def test_ordinary_google_sso_session_does_not_establish_recent_auth() -> None:
    user = User(id=uuid.uuid4(), email="person@example.com", password_hash="unused", password_enabled=False)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
    response = Response()
    db = SessionIssueDb()
    run(issue_session(
        db,
        user,
        request,
        response,
        authentication_method="google",
        establish_recent_auth=False,
    ))
    auth_session = next(item for item in db.added if isinstance(item, AuthSession))
    assert auth_session.reauthenticated_at is None


def test_public_cookie_contract_uses_host_prefix_secure_and_root_path() -> None:
    old = (settings.auth_cookie_name, settings.refresh_cookie_name, settings.auth_cookie_secure)
    settings.auth_cookie_name = "__Host-drivebound_session"
    settings.refresh_cookie_name = "__Host-drivebound_refresh"
    settings.auth_cookie_secure = True
    try:
        response = Response()
        set_access_cookie(response, "access")
        set_refresh_cookie(response, "refresh")
        cookies = [value.decode() for key, value in response.raw_headers if key.lower() == b"set-cookie"]
        assert len(cookies) == 2
        assert all("Path=/" in value and "Secure" in value and "Domain=" not in value for value in cookies)
    finally:
        settings.auth_cookie_name, settings.refresh_cookie_name, settings.auth_cookie_secure = old


def test_public_runtime_rejects_non_host_cookie_names() -> None:
    old = (settings.remote_access_enabled, settings.auth_cookie_name, settings.refresh_cookie_name)
    settings.remote_access_enabled = True
    settings.auth_cookie_name = "drivebound_session"
    settings.refresh_cookie_name = "drivebound_refresh"
    try:
        errors = runtime_configuration_errors()
        assert "AUTH_COOKIE_NAME must use the __Host- prefix for public access" in errors
        assert "REFRESH_COOKIE_NAME must use the __Host- prefix for public access" in errors
    finally:
        settings.remote_access_enabled, settings.auth_cookie_name, settings.refresh_cookie_name = old


def test_body_limit_rejects_control_json_but_preserves_media_upload_paths() -> None:
    client = TestClient(app)
    oversized = b"x" * (129 * 1024)
    assert client.post("/api/v1/auth/passkeys/login/options", content=oversized).status_code == 413
    assert client.post("/api/v1/sync/operations", content=oversized).status_code == 413
    assert client.post("/api/v1/assets/upload", content=oversized).status_code != 413


def test_public_browser_login_rejects_missing_and_cross_site_origin() -> None:
    old = settings.remote_access_enabled
    settings.remote_access_enabled = True
    try:
        client = TestClient(app)
        payload = {"email": "person@example.com", "password": "not-a-real-password"}
        missing = client.post("/api/v1/auth/login", json=payload)
        cross_site = client.post("/api/v1/auth/login", json=payload, headers={"Origin": "https://evil.example"})
        assert missing.status_code == 403
        assert cross_site.status_code == 403
        assert "set-cookie" not in missing.headers
        assert "set-cookie" not in cross_site.headers
    finally:
        settings.remote_access_enabled = old


class FakeSmtp:
    context = None

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self, *, context):
        type(self).context = context

    def login(self, *_args):
        pass

    def send_message(self, _message):
        pass


def test_smtp_starttls_requires_hostname_and_certificate_validation(monkeypatch) -> None:
    old = (settings.smtp_host, settings.smtp_use_tls, settings.smtp_username)
    settings.smtp_host = "smtp.example.com"
    settings.smtp_use_tls = True
    settings.smtp_username = None
    monkeypatch.setattr(accounts.smtplib, "SMTP", FakeSmtp)
    try:
        accounts._send_email("person@example.com", "Security notice", "No secret")
        assert FakeSmtp.context is not None
        assert FakeSmtp.context.check_hostname is True
        assert FakeSmtp.context.verify_mode == ssl.CERT_REQUIRED
    finally:
        settings.smtp_host, settings.smtp_use_tls, settings.smtp_username = old


class MethodDb:
    def __init__(self, values):
        self.values = iter(values)

    async def scalar(self, _statement):
        return next(self.values)


def test_reauthentication_methods_are_account_specific_and_stable() -> None:
    user = User(id=uuid.uuid4(), email="person@example.com", password_hash="unused", password_enabled=True)
    old = (settings.google_client_id, settings.google_client_secret)
    settings.google_client_id = "client"
    settings.google_client_secret = "secret"
    try:
        assert run(reauthentication_methods(MethodDb([uuid.uuid4(), uuid.uuid4()]), user)) == [
            "password",
            "google",
            "passkey",
        ]
        settings.google_client_secret = None
        user.password_enabled = False
        # A linked identity is not an available method while the provider is
        # disabled; only the passkey query is performed in this branch.
        assert run(reauthentication_methods(MethodDb([None]), user)) == []
    finally:
        settings.google_client_id, settings.google_client_secret = old


class RateRedis:
    def __init__(self):
        self.counts = {}

    async def eval(self, _script, number_of_keys, *args):
        keys = args[:number_of_keys]
        values = []
        for key in keys:
            self.counts[key] = self.counts.get(key, 0) + 1
            values.append(self.counts[key])
        return values


def rate_request(ip: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": (ip, 1234)})


def test_username_less_rate_limit_is_ip_scoped_not_global(monkeypatch) -> None:
    redis = RateRedis()
    old_attempts = settings.auth_rate_limit_attempts
    settings.auth_rate_limit_attempts = 1
    monkeypatch.setattr(accounts, "auth_rate_redis", lambda _url: redis)
    try:
        attacker = rate_request("198.51.100.1")
        for _ in range(10):
            run(accounts.enforce_rate_limit(attacker, None))
        with pytest.raises(Exception) as blocked:
            run(accounts.enforce_rate_limit(attacker, None))
        assert blocked.value.status_code == 429
        # A constant-free IP-only key means one attacker cannot exhaust a
        # global identity bucket for a victim on another address.
        run(accounts.enforce_rate_limit(rate_request("203.0.113.2"), None))
    finally:
        settings.auth_rate_limit_attempts = old_attempts


def test_kdf_capacity_is_held_through_repeated_cancellation() -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()

    def blocking_work():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            release.wait(timeout=2)
            return "done"
        finally:
            with lock:
                active -= 1

    async def scenario():
        tasks = [asyncio.create_task(security._bounded_kdf(blocking_work)) for _ in range(6)]
        deadline = time.monotonic() + 1
        while active < 4 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert active == 4
        tasks[0].cancel()
        await asyncio.sleep(0)
        tasks[0].cancel()
        await asyncio.sleep(0.02)
        # The worker is still alive, so the cancelled task must retain its slot.
        assert active == 4
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert maximum <= 4


class CurrentAuthSession:
    def __init__(self, auth_session, user):
        self.auth_session = auth_session
        self.user = user

    async def scalar(self, _statement):
        return self.auth_session

    async def get(self, _model, _identifier):
        return self.user


class ActivitySession:
    def __init__(self):
        self.statement = None
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        self.statement = statement

    async def commit(self):
        self.committed = True


def test_last_seen_touch_rechecks_session_and_active_account(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    user = User(id=uuid.uuid4(), email="person@example.com", password_hash="unused")
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash="f" * 64,
        last_seen_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=1),
    )
    activity = ActivitySession()
    monkeypatch.setattr(security, "async_session_factory", lambda: activity)
    token = security.create_access_token(user.id, auth_session.id)
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    assert run(security.current_auth(request, token, CurrentAuthSession(auth_session, user))) == (
        user,
        auth_session,
    )
    assert activity.committed is True
    compiled = str(activity.statement.compile(dialect=postgresql.dialect())).lower()
    assert "auth_sessions.revoked_at is null" in compiled
    assert "auth_sessions.expires_at >" in compiled
    assert "auth_sessions.last_seen_at <" in compiled
    assert "exists (select *" in compiled
    assert "users.disabled_at is null" in compiled
