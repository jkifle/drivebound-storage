import os
import uuid

import pytest

from app.services.encryption import (
    MediaEncryptionError,
    decrypt_file,
    generate_user_media_key,
    unwrap_user_media_key,
    wrap_user_media_key,
)
from app.services.storage import original_path_for


def test_media_file_is_encrypted_and_round_trips(tmp_path):
    from app.services.encryption import encrypt_file

    source = tmp_path / "source.jpg"
    encrypted = tmp_path / "encrypted.media"
    restored = tmp_path / "restored.jpg"
    original = os.urandom(1024 * 1024 + 17)
    source.write_bytes(original)
    key = unwrap_user_media_key(wrap_user_media_key(generate_user_media_key()))

    encrypt_file(source, encrypted, key)
    decrypt_file(encrypted, restored, key)

    assert encrypted.read_bytes() != original
    assert restored.read_bytes() == original


def test_media_file_rejects_the_wrong_account_key(tmp_path):
    from app.services.encryption import encrypt_file

    source = tmp_path / "source.jpg"
    encrypted = tmp_path / "encrypted.media"
    source.write_bytes(b"private media")
    first_key = unwrap_user_media_key(wrap_user_media_key(generate_user_media_key()))
    second_key = unwrap_user_media_key(wrap_user_media_key(generate_user_media_key()))

    encrypt_file(source, encrypted, first_key)
    with pytest.raises(MediaEncryptionError):
        decrypt_file(encrypted, tmp_path / "wrong-key.jpg", second_key)


def test_encrypted_original_paths_are_account_scoped(monkeypatch, tmp_path):
    from app.services import storage

    monkeypatch.setattr(storage.settings, "originals_path", tmp_path)
    checksum = "b" * 64
    first = original_path_for(checksum, "same.jpg", uuid.UUID("00000000-0000-0000-0000-000000000001"))
    second = original_path_for(checksum, "same.jpg", uuid.UUID("00000000-0000-0000-0000-000000000002"))

    assert first != second
    assert first.name == checksum
    assert second.name == checksum
