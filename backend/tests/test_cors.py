from fastapi.testclient import TestClient

from app.main import app


def test_frontend_origin_can_preflight_upload():
    response = TestClient(app).options(
        "/api/v1/assets/upload",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_frontend_origin_can_preflight_account_deletion():
    response = TestClient(app).options(
        "/api/v1/auth/account",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]


def test_frontend_origin_can_preflight_storage_policy_update():
    response = TestClient(app).options(
        "/api/v1/storage/policy",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PUT",
        },
    )

    assert response.status_code == 200
    assert "PUT" in response.headers["access-control-allow-methods"]
