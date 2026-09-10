"""Host-approved storage identity and import grants for guided installations.

The installer owns this read-only manifest and same-volume markers. Account
requests can neither change the manifest nor approve a replacement drive.
Manual deployments without a manifest keep their existing storage semantics.
"""

import hmac
import json
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.models.user import User

StorageRole = Literal["imports", "originals", "derivatives", "staging", "replicas", "backups"]
STORAGE_ROLES: tuple[StorageRole, ...] = (
    "imports", "originals", "derivatives", "staging", "replicas", "backups",
)


class StorageUnavailableError(HTTPException):
    def __init__(self, role: str = "configured"):
        super().__init__(503, f"The {role} storage drive is unavailable or its identity could not be verified. Reconnect the approved drive and retry.")


class StorageBinding(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: StorageRole
    path: Path
    marker_path: Path
    marker: str = Field(min_length=16, max_length=256)


class StorageManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: Literal[1]
    installation_id: str = Field(min_length=1, max_length=128)
    owner_email: str = Field(min_length=3, max_length=320)
    import_display_name: str = Field(default="Photos selected on your PC", min_length=1, max_length=255)
    bindings: list[StorageBinding] = Field(min_length=6, max_length=6)


def load_storage_manifest() -> StorageManifest | None:
    source = settings.storage_manifest_path
    if source is None:
        return None
    try:
        if source.stat().st_size > 65536:
            raise ValueError("Oversized manifest")
        manifest = StorageManifest.model_validate_json(source.read_text(encoding="utf-8-sig"))
        if {binding.role for binding in manifest.bindings} != set(STORAGE_ROLES):
            raise ValueError("Incomplete bindings")
        expected = {
            "originals": settings.originals_path,
            "derivatives": settings.derivatives_path,
            "staging": settings.staging_path,
            "replicas": settings.replica_path,
            "backups": settings.backups_path,
        }
        for binding in manifest.bindings:
            if not binding.path.is_absolute() or not binding.marker_path.is_absolute():
                raise ValueError("Relative storage binding")
            if binding.role == "imports":
                if len(settings.external_root_list) != 1 or binding.path.resolve() != settings.external_root_list[0]:
                    raise ValueError("Import configuration does not match the installer")
            elif binding.path.resolve() != expected[binding.role].resolve():
                raise ValueError("Storage configuration does not match the installer")
        return manifest
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise StorageUnavailableError() from exc


def verify_binding(binding: StorageBinding) -> None:
    try:
        if not binding.path.is_dir() or not binding.marker_path.is_file():
            raise ValueError("Missing drive or marker")
        if binding.marker_path.stat().st_size > 1024:
            raise ValueError("Invalid marker")
        marker = binding.marker_path.read_text(encoding="utf-8-sig").strip()
        if not hmac.compare_digest(marker.encode("utf-8"), binding.marker.encode("utf-8")):
            raise ValueError("Wrong drive marker")
    except (OSError, ValueError) as exc:
        raise StorageUnavailableError(binding.role) from exc


def require_storage(*roles: StorageRole) -> None:
    manifest = load_storage_manifest()
    if manifest is not None:
        for binding in manifest.bindings:
            if not roles or binding.role in roles:
                verify_binding(binding)


def require_storage_path(path: Path | str) -> None:
    """Validate the owning mount before opening, creating, or removing bytes."""
    manifest = load_storage_manifest()
    if manifest is None:
        return
    resolved = Path(path).resolve()
    matches = [binding for binding in manifest.bindings if resolved.is_relative_to(binding.path.resolve())]
    if not matches:
        raise StorageUnavailableError()
    for binding in matches:
        verify_binding(binding)


def storage_readiness() -> dict[str, str]:
    try:
        manifest = load_storage_manifest()
    except StorageUnavailableError:
        return {role: "unavailable" for role in STORAGE_ROLES}
    if manifest is None:
        # Existing manual deployments are not falsely credited with verified
        # volume identity. Their path checks remain in the ordinary endpoints.
        return {"identity": "not_configured"}
    result: dict[str, str] = {}
    for binding in manifest.bindings:
        try:
            verify_binding(binding)
            result[binding.role] = "ok"
        except StorageUnavailableError:
            result[binding.role] = "unavailable"
    return result


def configured_owner_email() -> str | None:
    manifest = load_storage_manifest()
    email = manifest.owner_email if manifest is not None else settings.host_owner_email
    return email.strip().casefold() if email and email.strip() else None


def is_host_owner(user: User) -> bool:
    owner = configured_owner_email()
    return bool(owner and user.email_verified_at is not None and user.disabled_at is None and user.email.strip().casefold() == owner)


def require_host_owner(user: User) -> None:
    if configured_owner_email() is None:
        raise HTTPException(409, "The PC owner must approve an account in Drivebound Setup before connecting a host photo folder.")
    if not is_host_owner(user):
        raise HTTPException(403, "Only the verified account approved by the PC owner can connect its photo folder.")


def approved_import_root(user: User) -> Path:
    require_host_owner(user)
    manifest = load_storage_manifest()
    if manifest is not None:
        binding = next(binding for binding in manifest.bindings if binding.role == "imports")
        verify_binding(binding)
        return binding.path.resolve()
    roots = settings.external_root_list
    if len(roots) != 1:
        raise HTTPException(409, "The host operator must select one photo folder for guided connection.")
    if not roots[0].is_dir():
        raise StorageUnavailableError("imports")
    return roots[0]
