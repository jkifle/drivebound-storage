import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.observability import metrics
from app.main import app, runtime_configuration_errors


def test_metrics_require_the_configured_bearer_token() -> None:
    original = settings.metrics_auth_token
    settings.metrics_auth_token = "m" * 40
    try:
        client = TestClient(app)
        missing = client.get("/metrics")
        wrong = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
        accepted = client.get("/metrics", headers={"Authorization": f"Bearer {'m' * 40}"})

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert wrong.status_code == 401
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert "drivebound_http_requests_total" in accepted.text
    finally:
        settings.metrics_auth_token = original


def test_request_metrics_use_route_templates_instead_of_resource_ids() -> None:
    asset_id = uuid.uuid4()

    response = TestClient(app).get(f"/api/v1/assets/{asset_id}/versions")

    assert response.status_code == 401
    output = metrics.prometheus()
    assert str(asset_id) not in output
    assert 'path="/assets/{asset_id}/versions"' in output


def test_remote_access_rejects_an_unprotected_metrics_endpoint() -> None:
    original_remote = settings.remote_access_enabled
    original_token = settings.metrics_auth_token
    settings.remote_access_enabled = True
    settings.metrics_auth_token = None
    try:
        assert "METRICS_AUTH_TOKEN must be a long random value" in runtime_configuration_errors()
    finally:
        settings.remote_access_enabled = original_remote
        settings.metrics_auth_token = original_token
