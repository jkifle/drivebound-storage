"""Header-only regressions; no storage, database, or decryption is needed."""

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.services import media_delivery


@pytest.fixture(params=[None, 1], ids=["plaintext", "encrypted"])
def response_for(request, monkeypatch):
    monkeypatch.setattr(media_delivery, "user_media_key", lambda owner: b"test-key")
    monkeypatch.setattr(media_delivery, "iter_decrypted_chunks", lambda path, key: iter([b"fixture"]))
    asset = SimpleNamespace(encryption_version=request.param, mime_type="image/jpeg", file_size=7)

    def make(filename, disposition="attachment", media_type="image/jpeg"):
        return media_delivery.media_response(
            asset, SimpleNamespace(), Path("not-read.jpg"), media_type=media_type,
            filename=filename, content_disposition_type=disposition,
        )

    return make


@pytest.mark.parametrize("disposition", ["attachment", "inline"])
def test_ascii_filename_retains_quoted_header(response_for, disposition):
    response = response_for("photo-2026.jpg", disposition)
    assert response.headers["content-disposition"] == f'{disposition}; filename="photo-2026.jpg"'


@pytest.mark.parametrize("filename", [
    "照片-旅行.jpg", "café.jpg", "family photo.jpg", 'photo"quoted".jpg',
    "photo'quoted'.jpg", r"folder\photo.jpg", "folder/photo.jpg", "photo%0D%0A.jpg",
])
def test_special_filename_uses_utf8_extended_parameter(response_for, filename):
    response = response_for(filename)
    value = response.headers["content-disposition"]
    prefix = "attachment; filename*=utf-8''"
    assert value.startswith(prefix)
    assert unquote(value.removeprefix(prefix)) == filename
    assert value.isascii()
    assert all(byte < 128 for name, raw in response.raw_headers if name == b"content-disposition" for byte in raw)
    assert '"' not in value


@pytest.mark.parametrize("filename, cleaned", [
    ("photo\r\nX-Injected: yes.jpg", "photoX-Injected: yes.jpg"),
    ("photo\x00\t\x1f\x7f.jpg", "photo.jpg"),
    ("\r\n\x00", "download"),
    ('照片"\r\n.jpg', '照片".jpg'),
])
def test_controls_cannot_create_headers_or_survive_decoding(response_for, filename, cleaned):
    response = response_for(filename)
    value = response.headers["content-disposition"]
    assert "\r" not in value and "\n" not in value
    assert "x-injected" not in response.headers
    assert len([name for name, _ in response.raw_headers if name == b"content-disposition"]) == 1
    if "filename*=" in value:
        decoded = unquote(value.split("filename*=utf-8''", 1)[1])
    else:
        decoded = value.split('filename="', 1)[1][:-1]
    assert decoded == cleaned
    assert all(ord(character) >= 32 and ord(character) != 127 for character in decoded)


@pytest.mark.parametrize("disposition", ["attachment\r\nX-Injected: yes", "other"])
def test_disposition_rejects_untrusted_header_values(response_for, disposition):
    with pytest.raises(ValueError, match="Unsupported content disposition"):
        response_for("photo.jpg", disposition)


def test_no_filename_does_not_add_a_download_header(response_for):
    assert "content-disposition" not in response_for(None).headers


def test_encrypted_original_preserves_plaintext_length(monkeypatch):
    monkeypatch.setattr(media_delivery, "user_media_key", lambda owner: b"test-key")
    monkeypatch.setattr(media_delivery, "iter_decrypted_chunks", lambda path, key: iter([]))
    asset = SimpleNamespace(encryption_version=1, mime_type="image/jpeg", file_size=42)
    response = media_delivery.media_response(
        asset, SimpleNamespace(), Path("not-read.media"), media_type="image/jpeg", filename="照片.jpg",
    )
    assert response.headers["content-length"] == "42"
    derivative = media_delivery.media_response(
        asset, SimpleNamespace(), Path("not-read.media"), media_type="image/webp", filename="照片.webp",
    )
    assert "content-length" not in derivative.headers


def test_frontend_build_context_excludes_local_secrets_and_state():
    """Guard required Docker patterns, without reading any credential files."""
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    patterns = {
        line.strip() for line in (frontend / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        ".env*", "**/.env*", ".npmrc", "**/.npmrc", ".aws", "**/.aws", ".ssh", "**/.ssh",
        "*.pem", "**/*.pem", "*.key", "**/*.key", "*.p12", "**/*.p12", "*.pfx", "**/*.pfx",
        "*credentials*.json", "**/*credentials*.json", "*service-account*.json", "**/*service-account*.json",
        "*firebase-adminsdk*.json", "**/*firebase-adminsdk*.json", ".git", ".codex", ".agents",
        ".drivebound", "node_modules", ".next", "dist", ".wrangler", ".cache", "**/.cache",
        ".turbo", ".vercel", "coverage", "playwright-report", "test-results", "*.log", "**/*.log",
        "*.tsbuildinfo", "**/*.tsbuildinfo",
    } <= patterns
    assert not any(pattern.startswith("!") for pattern in patterns), "Negations can re-include secret files"
    assert not {"*", "*.json", "package.json", "package-lock.json", "app", "public"} & patterns
