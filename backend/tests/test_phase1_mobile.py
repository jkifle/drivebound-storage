from app.main import app
from app.schemas.device import BackupEventNotification, DevicePushTokenUpdate
from app.services.notifications import is_expo_push_token


def test_expo_push_tokens_are_recognized_without_storing_device_secrets():
    assert is_expo_push_token("ExponentPushToken[abc123]")
    assert is_expo_push_token("ExpoPushToken[abc123]")
    assert not is_expo_push_token("not-a-push-token")
    assert DevicePushTokenUpdate(token="ExpoPushToken[abc123]").token


def test_backup_notification_contract_is_limited_to_client_safe_events():
    completed = BackupEventNotification(kind="completed", uploaded=3, skipped=2)
    assert completed.uploaded == 3
    assert completed.skipped == 2


def test_mobile_device_and_library_routes_are_present():
    paths = app.openapi()["paths"]
    assert "/api/v1/devices/push-token" in paths
    assert "/api/v1/devices/backup-events" in paths
    assert "/api/v1/assets/{asset_id}/original" in paths
    assert "/api/v1/map" in paths
