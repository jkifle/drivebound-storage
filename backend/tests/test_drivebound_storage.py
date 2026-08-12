import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.v1.storage_status import inspect_root
from app.api.v1.uploads import session_response
from app.models.upload_session import UploadSession
from app.schemas.upload import UploadSessionCreate
from app.services import storage


def test_external_paths_must_stay_within_configured_roots(monkeypatch, tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    inside = root / "photos"
    inside.mkdir()
    monkeypatch.setattr(storage.settings, "external_library_roots", str(root))

    assert storage.validated_external_path(str(inside)) == inside.resolve()
    with pytest.raises(HTTPException) as error:
        storage.validated_external_path(str(tmp_path / "outside"))
    assert error.value.status_code == 400


def test_storage_inspection_reports_capacity(tmp_path):
    result = inspect_root("Test drive", tmp_path, "managed")
    assert result["status"] == "online"
    assert result["total_bytes"] > 0
    assert 0 <= result["used_percent"] <= 100


def test_upload_session_response_exposes_resume_contract(tmp_path):
    upload_id = uuid.uuid4()
    upload = UploadSession(
        id=upload_id,
        user_id=uuid.uuid4(),
        filename="video.mp4",
        mime_type="video/mp4",
        total_size=100,
        offset=40,
        staging_path=str(tmp_path / "upload"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        status="active",
    )
    response = session_response(upload)
    assert response.upload_url == f"/api/v1/uploads/{upload_id}"
    assert response.offset == 40
    assert response.chunk_size > 0


def test_upload_checksum_validation():
    with pytest.raises(ValueError):
        UploadSessionCreate(
            filename="photo.jpg",
            total_size=10,
            checksum="not-sha256",
        )


def test_upload_contract_accepts_original_file_timestamp():
    modified = datetime(2024, 5, 6, 7, 8, tzinfo=timezone.utc)
    payload = UploadSessionCreate(
        filename="original.jpg",
        total_size=10,
        file_modified_at=modified,
    )
    assert payload.file_modified_at == modified
