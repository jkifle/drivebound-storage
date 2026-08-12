from pathlib import Path

import pytest

from app.services import storage


def test_commit_original_never_overwrites(tmp_path):
    staged_one = tmp_path / "one.upload"
    staged_two = tmp_path / "two.upload"
    final = tmp_path / "originals" / "asset.jpg"
    staged_one.write_bytes(b"first")
    staged_two.write_bytes(b"second")

    assert storage.commit_original(staged_one, final) is True
    assert storage.commit_original(staged_two, final) is False
    assert final.read_bytes() == b"first"


@pytest.mark.parametrize("filename", ["photo.JPG", "same-content.png", "unsafe.exe!"])
def test_original_path_is_checksum_addressed(monkeypatch, tmp_path, filename):
    monkeypatch.setattr(storage.settings, "originals_path", tmp_path)
    checksum = "a" * 64
    assert storage.original_path_for(checksum, filename) == Path(tmp_path / "aa" / checksum)
