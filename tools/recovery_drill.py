#!/usr/bin/env python3
"""Emit redacted structural evidence for approved recovery inputs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.observability import metrics  # noqa: E402
from app.services.recovery_drill import RecoveryDrillManifestError, run_recovery_drill  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Create structural recovery-input evidence without restoring or modifying data."
    )
    result.add_argument("manifest", type=Path, help="Structural recovery-evidence manifest JSON")
    destination = result.add_mutually_exclusive_group()
    destination.add_argument("--report", type=Path, help="Create this exact JSON report; stdout is used when omitted")
    destination.add_argument(
        "--report-dir",
        type=Path,
        help="Create an exclusive timestamped JSON report in this existing durable directory",
    )
    result.add_argument(
        "--publish-metric",
        action="store_true",
        help="Publish only the aggregate structural-evidence outcome to the configured metrics Redis store",
    )
    return result


def _write_new_report(path: Path, encoded: bytes, protected_paths: set[Path]) -> None:
    output = path.resolve(strict=False)
    if output in protected_paths:
        raise RecoveryDrillManifestError("report path cannot replace a manifest or recovery input")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        output.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _timestamped_report_path(directory: Path, report: dict[str, object]) -> Path:
    resolved = directory.resolve(strict=True)
    if not resolved.is_dir():
        raise RecoveryDrillManifestError("report directory must be an existing directory")
    try:
        generated_at = datetime.fromisoformat(str(report["generated_at"]))
        evidence_id = str(report["evidence_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryDrillManifestError("report is missing its generated timestamp or evidence identifier") from exc
    timestamp = generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    return resolved / f"{evidence_id}-{timestamp}.json"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest, report = run_recovery_drill(args.manifest)
        encoded = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if args.report or args.report_dir:
            protected = {args.manifest.resolve(strict=False), *(item.path for item in manifest.inputs)}
            report_path = args.report or _timestamped_report_path(args.report_dir, report)
            _write_new_report(report_path, encoded, protected)
        else:
            sys.stdout.buffer.write(encoded)
        if args.publish_metric:
            published = metrics.operations.observe_recovery(
                "evidence_verification", "success" if report["overall"] == "passed" else "failed"
            )
            if not published:
                print("recovery evidence error: metrics Redis did not accept the aggregate outcome", file=sys.stderr)
                return 2
        return 0 if report["overall"] == "passed" else 1
    except RecoveryDrillManifestError as exc:
        print(f"recovery evidence error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        # OSError text commonly embeds absolute host paths. Keep scheduler logs
        # useful without copying those paths into centralized evidence logs.
        print("recovery evidence error: filesystem operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
