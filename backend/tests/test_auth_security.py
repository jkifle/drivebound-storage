import uuid

from fastapi.testclient import TestClient

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.core.config import settings
from app.main import app


def test_passwords_are_argon2_hashed_and_verified():
    encoded = hash_password("a genuinely long password")

    assert encoded.startswith("$argon2")
    assert verify_password("a genuinely long password", encoded)
    assert not verify_password("the wrong password", encoded)


def test_access_token_round_trip():
    user_id = uuid.uuid4()

    assert decode_access_token(create_access_token(user_id)) == user_id


def test_private_library_routes_require_authentication():
    client = TestClient(app)

    assert client.get("/api/v1/assets").status_code == 401
    assert client.get("/api/v1/storage").status_code == 401
    assert client.get("/api/v1/libraries").status_code == 401


def test_auth_provider_status_is_public_and_safe_by_default():
    original_id, original_secret = settings.google_client_id, settings.google_client_secret
    settings.google_client_id = settings.google_client_secret = None
    try:
        response = TestClient(app).get("/api/v1/auth/providers")
        assert response.status_code == 200
        assert response.json()["google"] is False
        assert response.json()["google_start_url"].endswith("/api/v1/auth/google/start")
    finally:
        settings.google_client_id, settings.google_client_secret = original_id, original_secret


def test_unconfigured_google_login_does_not_redirect():
    original_id, original_secret = settings.google_client_id, settings.google_client_secret
    settings.google_client_id = settings.google_client_secret = None
    try:
        response = TestClient(app).get("/api/v1/auth/google/start")
        assert response.status_code == 503
    finally:
        settings.google_client_id, settings.google_client_secret = original_id, original_secret
