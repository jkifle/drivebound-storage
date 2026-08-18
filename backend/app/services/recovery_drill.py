"""Non-destructive recovery-input verification and redacted reporting."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.services.operational_backups import _verify_configuration_snapshot, _verify_database_dump
from app.services.storage import sha256_file


MAX_MANIFEST_BYTES = 64 * 1024
MAX_MEDIA_SAMPLES = 25
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}$")


class RecoveryDrillManifestError(ValueError):
    """The operator-supplied recovery manifest is unsafe or malformed."""


@dataclass(frozen=True)
class RecoveryInput:
    kind: str
    display_name: str
    path: Path
    expected_sha256: str


@dataclass(frozen=True)
class RecoveryDrillManifest:
    evidence_id: str
    release: str
    database: RecoveryInput
    configuration: RecoveryInput
    media_samples: tuple[RecoveryInput, ...]

    @property
    def inputs(self) -> tuple[RecoveryInput, ...]:
        return (self.database, self.configuration, *self.media_samples)


def _exact_keys(value: object, required: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != required:
        raise RecoveryDrillManifestError(f"{context} must contain exactly: {', '.join(sorted(required))}")
    return value


def _input(value: object, *, kind: str, manifest_root: Path, index: int | None = None) -> RecoveryInput:
    document = _exact_keys(value, {"path", "sha256"}, f"{kind} input")
    raw_path = document["path"]
    digest = document["sha256"]
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise RecoveryDrillManifestError(f"{kind} path must be a non-empty string")
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        raise RecoveryDrillManifestError(f"{kind} sha256 must be 64 lowercase hexadecimal characters")
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_root / path
    resolved = path.resolve(strict=False)
    # Reports use stable aliases rather than host filenames; even a basename can
    # contain a customer name or other deployment detail.
    display_name = f"media-sample-{index}" if kind == "media" else f"{kind}-archive"
    return RecoveryInput(kind=kind, display_name=display_name, path=resolved, expected_sha256=digest)


def load_recovery_drill_manifest(path: Path) -> RecoveryDrillManifest:
    manifest_path = path.resolve(strict=True)
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RecoveryDrillManifestError("manifest must be a regular JSON file no larger than 64 KiB")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryDrillManifestError("manifest is not readable UTF-8 JSON") from exc
    document = _exact_keys(
        raw,
        {"schema", "evidence_id", "release", "database", "configuration", "media_samples"},
        "manifest",
    )
    if document["schema"] != 1:
        raise RecoveryDrillManifestError("manifest schema must be 1")
    evidence_id = document["evidence_id"]
    release = document["release"]
    if not isinstance(evidence_id, str) or IDENTIFIER_PATTERN.fullmatch(evidence_id) is None:
        raise RecoveryDrillManifestError("evidence_id must be a short non-secret identifier")
    if not isinstance(release, str) or RELEASE_PATTERN.fullmatch(release) is None:
        raise RecoveryDrillManifestError("release must be a short immutable release identifier")
    media_values = document["media_samples"]
    if not isinstance(media_values, list) or not 1 <= len(media_values) <= MAX_MEDIA_SAMPLES:
        raise RecoveryDrillManifestError("media_samples must contain between 1 and 25 entries")
    root = manifest_path.parent
    database = _input(document["database"], kind="database", manifest_root=root)
    configuration = _input(document["configuration"], kind="configuration", manifest_root=root)
    media = tuple(
        _input(value, kind="media", manifest_root=root, index=index)
        for index, value in enumerate(media_values, start=1)
    )
    inputs = (database, configuration, *media)
    if len({item.path for item in inputs}) != len(inputs):
        raise RecoveryDrillManifestError("every recovery input path must be unique")
    if manifest_path in {item.path for item in inputs}:
        raise RecoveryDrillManifestError("the manifest cannot also be a recovery input")
    return RecoveryDrillManifest(
        evidence_id=evidence_id,
        release=release,
        database=database,
        configuration=configuration,
        media_samples=media,
    )


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "unreadable"
    if isinstance(exc, IsADirectoryError):
        return "not_a_regular_file"
    if isinstance(exc, RuntimeError):
        return "verification_tool_unavailable"
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return "invalid_format"
    return "verification_failed"


def _verify_input(
    item: RecoveryInput,
    *,
    database_verifier: Callable[[Path], str],
    configuration_verifier: Callable[[Path], str],
) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": item.kind,
        "name": item.display_name,
        "status": "failed",
        "size_bytes": 0,
        "checks": [],
    }
    try:
        before = item.path.stat()
        if not item.path.is_file():
            raise IsADirectoryError
        result["size_bytes"] = before.st_size
        actual = sha256_file(item.path)
        if actual != item.expected_sha256:
            result["failure_code"] = "checksum_mismatch"
            return result
        checks = ["sha256"]
        if item.kind == "database":
            database_verifier(item.path)
            checks.append("pg_restore_list")
        elif item.kind == "configuration":
            configuration_verifier(item.path)
            checks.append("secret_free_configuration")
        after = item.path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            result["failure_code"] = "changed_during_verification"
            return result
        result.update(status="passed", checks=checks)
        return result
    except Exception as exc:
        result["failure_code"] = _failure_code(exc)
        return result


def run_recovery_drill(
    manifest_path: Path,
    *,
    database_verifier: Callable[[Path], str] = _verify_database_dump,
    configuration_verifier: Callable[[Path], str] = _verify_configuration_snapshot,
) -> tuple[RecoveryDrillManifest, dict[str, object]]:
    """Verify recovery inputs without opening a database or writing media."""
    started_at = time.perf_counter()
    manifest = load_recovery_drill_manifest(manifest_path)
    checks = [
        _verify_input(
            item,
            database_verifier=database_verifier,
            configuration_verifier=configuration_verifier,
        )
        for item in manifest.inputs
    ]
    passed = sum(check["status"] == "passed" for check in checks)
    report: dict[str, object] = {
        "schema": 1,
        "mode": "verify_only",
        "evidence_scope": "structural_recovery_inputs_only",
        "evidence_id": manifest.evidence_id,
        "release": manifest.release,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "overall": "passed" if passed == len(checks) else "failed",
        "summary": {"passed": passed, "failed": len(checks) - passed, "total": len(checks)},
        "checks": checks,
        "safety": {
            "database_restore_attempted": False,
            "isolated_restore_attempted": False,
            "application_recovery_proven": False,
            "media_writes_attempted": False,
            "paths_redacted": True,
        },
    }
    return manifest, report
