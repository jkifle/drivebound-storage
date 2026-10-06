import asyncio
import errno
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.v1 import uploads


@pytest.mark.parametrize("error, expected", [
    (OSError(errno.ENOSPC, "private-path"), "drive is full"),
    (PermissionError(errno.EACCES, "private-path"), "permissions"),
    (OSError(errno.ENOTSUP, "private-path"), "filesystem"),
    (RuntimeError("private-path"), "could not finish"),
])
def test_failed_finalization_keeps_staging_and_returns_safe_diagnostic(tmp_path, monkeypatch, caplog, error, expected):
    staged = tmp_path / "test.resumable"
    staged.write_bytes(b"retained bytes")
    upload = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), total_size=14,
                             mime_type="image/jpeg", filename="private-name.jpg",
                             file_created_at=None, file_modified_at=None)
    monkeypatch.setattr(uploads, "persist_managed_asset", AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(uploads.finalize_upload_asset(None, upload, staged, "a" * 64))
    assert failure.value.status_code == 503
    assert expected in failure.value.detail
    assert str(upload.id) in failure.value.detail
    assert "private-path" not in failure.value.detail + caplog.text
    assert "private-name" not in failure.value.detail + caplog.text
    assert staged.read_bytes() == b"retained bytes"


def test_storage_identity_error_keeps_original_actionable_message(monkeypatch, tmp_path):
    error = HTTPException(503, "Reconnect the approved drive")
    monkeypatch.setattr(uploads, "persist_managed_asset", AsyncMock(side_effect=error))
    upload = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), total_size=1,
                             mime_type="image/jpeg", filename="test.jpg",
                             file_created_at=None, file_modified_at=None)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(uploads.finalize_upload_asset(None, upload, tmp_path / "upload", "a" * 64))
    assert failure.value is error


def test_empty_patch_finalizes_received_bytes_and_preserves_metadata(monkeypatch, tmp_path):
    from starlette.requests import Request
    from app.services.storage import sha256_file

    staged = tmp_path / "received.resumable"
    staged.write_bytes(b"complete file")
    modified = datetime.now(timezone.utc)
    upload = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), device_id=None,
                             total_size=13, offset=13, status="active", expected_checksum=None,
                             expires_at=modified + timedelta(hours=1), staging_path=str(staged),
                             mime_type="image/jpeg", filename="original.jpg",
                             file_created_at=modified, file_modified_at=modified)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), upload]),
                              get=AsyncMock(return_value=upload), commit=AsyncMock())
    persist = AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), False))
    monkeypatch.setattr(uploads, "persist_managed_asset", persist)
    monkeypatch.setattr(uploads, "require_storage_path", lambda _: None)
    monkeypatch.setattr(uploads.process_asset_task, "delay", Mock())
    monkeypatch.setattr(uploads, "session_response", lambda item, *_: item.status)
    request = Request({"type": "http", "method": "PATCH"})
    result = asyncio.run(uploads.append_upload(upload.id, request, 13, session, uploads.UploadPrincipal(upload.user_id)))
    assert result == "complete"
    assert persist.call_args.kwargs["checksum"] == sha256_file(staged)
    assert persist.call_args.kwargs["file_modified_at"] == modified
    assert persist.call_args.kwargs["file_created_at"] == modified
    assert persist.call_args.kwargs["filename"] == "original.jpg"
