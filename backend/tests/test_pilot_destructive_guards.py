"""Pilot acceptance: unavailable disks must not erase cleanup evidence."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import main
from app.core.config import Settings, settings
from app.models.asset import Asset
from app.services import account_deletion, lifecycle, storage
from app.services.host_storage import STORAGE_ROLES, StorageUnavailableError


@pytest.fixture
def approved_storage(monkeypatch, tmp_path):
    bindings = []
    paths = {}
    for role in STORAGE_ROLES:
        directory = tmp_path / role
        directory.mkdir()
        marker = directory / ".drivebound-volume"
        marker.write_text(f"test-approved-volume-{role}", encoding="utf-8")
        paths[role] = directory
        bindings.append({
            "role": role,
            "path": str(directory),
            "marker_path": str(marker),
            "marker": f"test-approved-volume-{role}",
        })
        if role != "imports":
            setting = "replica_path" if role == "replicas" else f"{role}_path"
            monkeypatch.setattr(settings, setting, directory)
    monkeypatch.setattr(settings, "external_library_roots", str(paths["imports"]))
    monkeypatch.setattr(settings, "replica_roots", str(paths["replicas"]))
    manifest = tmp_path / "storage.json"
    manifest.write_text(json.dumps({
        "version": 1, "installation_id": "pilot-acceptance",
        "owner_email": "owner@example.test", "bindings": bindings,
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "storage_manifest_path", manifest)
    return paths


def break_marker(paths, role, failure):
    marker = paths[role] / ".drivebound-volume"
    if failure == "missing":
        marker.unlink()
    else:
        marker.write_text("another-disk-with-the-same-folder", encoding="utf-8")


class UntouchedSession:
    """Storage preflight must fail before querying or changing cleanup state."""

    def __getattr__(self, name):
        raise AssertionError(f"Unavailable disk unexpectedly reached database operation: {name}")


@pytest.mark.parametrize("failure", ["missing", "wrong"])
def test_missing_original_on_unapproved_volume_cannot_be_counted_as_unlinked(approved_storage, failure):
    missing_original = approved_storage["originals"] / "nonexistent-object"
    break_marker(approved_storage, "originals", failure)

    with pytest.raises(StorageUnavailableError) as error:
        asyncio.run(lifecycle.safely_unlink_catalog_path(
            UntouchedSession(), storage.canonical_storage_path(missing_original),
            root=approved_storage["originals"],
        ))

    assert error.value.status_code == 503
    assert not missing_original.exists()


@pytest.mark.parametrize("role", ["originals", "derivatives", "replicas"])
@pytest.mark.parametrize("failure", ["missing", "wrong"])
def test_purge_keeps_asset_tombstone_before_unavailable_disk_cleanup(approved_storage, role, failure):
    now = datetime.now(timezone.utc)
    original = approved_storage["originals"] / "original"
    original.write_bytes(b"disposable managed original")
    asset = Asset(
        id=uuid.uuid4(), user_id=uuid.uuid4(), logical_id=uuid.uuid4(),
        original_path=storage.canonical_storage_path(original), storage_source="managed",
        checksum="a" * 64, storage_checksum="b" * 64,
        mime_type="image/jpeg", file_size=27,
        lifecycle_state="trashed", trashed_at=now - timedelta(days=2),
        purge_after=now - timedelta(days=1),
    )
    break_marker(approved_storage, role, failure)

    with pytest.raises(StorageUnavailableError):
        asyncio.run(lifecycle.safely_purge_asset(UntouchedSession(), asset))

    assert asset.lifecycle_state == "trashed"
    assert original.read_bytes() == b"disposable managed original"


@pytest.mark.parametrize("role", ["originals", "derivatives", "staging", "replicas"])
@pytest.mark.parametrize("failure", ["missing", "wrong"])
def test_account_orphan_sweep_cannot_certify_empty_replacement_storage(approved_storage, role, failure):
    subject = uuid.uuid4()
    original = approved_storage["originals"] / str(subject) / "orphan"
    original.parent.mkdir()
    original.write_bytes(b"retain until all relevant volumes are verified")
    break_marker(approved_storage, role, failure)

    with pytest.raises(StorageUnavailableError):
        asyncio.run(account_deletion.sweep_account_namespaces(UntouchedSession(), subject))

    assert original.read_bytes() == b"retain until all relevant volumes are verified"


@pytest.mark.parametrize("changes, expected_error", [
    ({"email_delivery_mode": "console"}, "public access requires EMAIL_DELIVERY_MODE=smtp and SMTP_HOST"),
    ({"smtp_host": None}, "public access requires EMAIL_DELIVERY_MODE=smtp and SMTP_HOST"),
    ({"smtp_use_tls": False}, "public SMTP delivery requires SMTP_USE_TLS=true"),
    ({"auth_cookie_secure": False}, "AUTH_COOKIE_SECURE must be true"),
    ({"auth_cookie_name": "local_cookie"}, "AUTH_COOKIE_NAME must use the __Host- prefix for public access"),
    ({"cors_origins": "*"}, "CORS_ORIGINS cannot contain a wildcard"),
])
def test_private_remote_pilot_cannot_bypass_existing_security_gates(monkeypatch, tmp_path, changes, expected_error):
    configured = Settings(
        _env_file=None, environment="development", remote_access_enabled=True,
        app_url="https://drivebound.pilot.ts.net", api_url="https://drivebound.pilot.ts.net:8443",
        auth_cookie_secure=True, auth_cookie_name="__Host-drivebound_session",
        refresh_cookie_name="__Host-drivebound_refresh", cors_origins="https://drivebound.pilot.ts.net",
        trusted_hosts="localhost,127.0.0.1,drivebound.pilot.ts.net", trusted_proxy_cidrs="",
        jwt_secret="j" * 64, mfa_encryption_secret="m" * 64, metrics_auth_token="t" * 64,
        email_delivery_mode="smtp", smtp_host="smtp.example.test", smtp_use_tls=True,
        backups_path=tmp_path / "backups", storage_manifest_path=None,
    )
    configured.account_deletion_ledger_path.mkdir(parents=True)
    monkeypatch.setattr(main, "settings", configured)
    assert main.runtime_configuration_errors() == []
    for name, value in changes.items():
        setattr(configured, name, value)

    assert expected_error in main.runtime_configuration_errors()
