"""Envelope encryption for media stored by Drivebound.

The media encryption master key is supplied by the deployment's secret store.
Each account receives a random data-encryption key (DEK), which is wrapped by
that master key. Files use the account DEK with AES-256-GCM; the encrypted file
format is private to Drivebound and carries only a version marker and nonce.
"""

import base64
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.config import settings

FORMAT_MAGIC = b"DRIVEBOUND-ENC-V1\x00"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 4 * 1024 * 1024


class MediaEncryptionError(ValueError):
    """Raised when a stored encrypted media object cannot be authenticated."""


def generate_user_media_key() -> str:
    """Return a URL-safe base64-encoded AES-256 key for one account."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def wrap_user_media_key(key: str) -> str:
    return Fernet(settings.media_encryption_key).encrypt(key.encode("ascii")).decode("ascii")


def unwrap_user_media_key(wrapped_key: str) -> bytes:
    try:
        encoded_key = Fernet(settings.media_encryption_key).decrypt(wrapped_key.encode("ascii"))
        key = base64.urlsafe_b64decode(encoded_key)
    except (InvalidToken, ValueError) as exc:
        raise MediaEncryptionError("User media key cannot be unwrapped") from exc
    if len(key) != 32:
        raise MediaEncryptionError("User media key has an invalid length")
    return key


def _temporary_path(destination: Path, action: str) -> Path:
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.{action}")


def encrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Atomically encrypt ``source`` to ``destination`` without loading it all.

    The destination is never overwritten. A pre-existing destination is treated
    as a completed concurrent write and raises ``FileExistsError``.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination, "encrypt")
    nonce = os.urandom(NONCE_SIZE)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            output_file.write(FORMAT_MAGIC)
            output_file.write(nonce)
            while chunk := input_file.read(CHUNK_SIZE):
                output_file.write(encryptor.update(chunk))
            output_file.write(encryptor.finalize())
            output_file.write(encryptor.tag)
            output_file.flush()
            os.fsync(output_file.fileno())
        try:
            # os.replace would silently overwrite a concurrent upload. Publish
            # through a hard link instead, which is atomic and exclusive.
            os.link(temporary, destination)
        except FileExistsError:
            raise
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def decrypt_file(source: Path, destination: Path, key: bytes) -> None:
    """Authenticate and atomically decrypt a Drivebound encrypted file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination, "decrypt")
    try:
        with source.open("rb") as input_file:
            header = input_file.read(len(FORMAT_MAGIC) + NONCE_SIZE)
            if len(header) != len(FORMAT_MAGIC) + NONCE_SIZE or not header.startswith(FORMAT_MAGIC):
                raise MediaEncryptionError("Stored media has an unknown encryption format")
            nonce = header[len(FORMAT_MAGIC):]
            total_size = source.stat().st_size
            ciphertext_size = total_size - len(FORMAT_MAGIC) - NONCE_SIZE - TAG_SIZE
            if ciphertext_size < 0:
                raise MediaEncryptionError("Stored media is truncated")
            input_file.seek(total_size - TAG_SIZE)
            tag = input_file.read(TAG_SIZE)
            input_file.seek(len(FORMAT_MAGIC) + NONCE_SIZE)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            remaining = ciphertext_size
            with temporary.open("xb") as output_file:
                while remaining:
                    chunk = input_file.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise MediaEncryptionError("Stored media is truncated")
                    remaining -= len(chunk)
                    output_file.write(decryptor.update(chunk))
                try:
                    output_file.write(decryptor.finalize())
                except InvalidTag as exc:
                    raise MediaEncryptionError("Stored media failed authentication") from exc
                output_file.flush()
                os.fsync(output_file.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def iter_decrypted_chunks(source: Path, key: bytes) -> Iterator[bytes]:
    """Yield authenticated-by-GCM-on-completion plaintext for HTTP streaming."""
    with source.open("rb") as input_file:
        header = input_file.read(len(FORMAT_MAGIC) + NONCE_SIZE)
        if len(header) != len(FORMAT_MAGIC) + NONCE_SIZE or not header.startswith(FORMAT_MAGIC):
            raise MediaEncryptionError("Stored media has an unknown encryption format")
        nonce = header[len(FORMAT_MAGIC):]
        total_size = source.stat().st_size
        ciphertext_size = total_size - len(FORMAT_MAGIC) - NONCE_SIZE - TAG_SIZE
        if ciphertext_size < 0:
            raise MediaEncryptionError("Stored media is truncated")
        input_file.seek(total_size - TAG_SIZE)
        tag = input_file.read(TAG_SIZE)
        input_file.seek(len(FORMAT_MAGIC) + NONCE_SIZE)
        decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        remaining = ciphertext_size
        while remaining:
            chunk = input_file.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise MediaEncryptionError("Stored media is truncated")
            remaining -= len(chunk)
            plaintext = decryptor.update(chunk)
            if plaintext:
                yield plaintext
        try:
            final = decryptor.finalize()
        except InvalidTag as exc:
            raise MediaEncryptionError("Stored media failed authentication") from exc
        if final:
            yield final
