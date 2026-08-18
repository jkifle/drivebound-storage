#!/usr/bin/env python3
"""Scheduler-safe wrapper for retained structural recovery evidence.

Arguments take precedence over the matching environment variables. This
wrapper invokes the verifier without a shell and always requires a durable
report directory, making it suitable for Task Scheduler, cron, or a CI runner
that mounts operator-approved recovery inputs read-only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run retained structural recovery-input verification evidence.")
    result.add_argument("--manifest", type=Path, default=os.getenv("DRIVEBOUND_RECOVERY_MANIFEST"))
    result.add_argument("--report-dir", type=Path, default=os.getenv("DRIVEBOUND_RECOVERY_REPORT_DIR"))
    result.add_argument(
        "--publish-metric",
        action="store_true",
        default=_true(os.getenv("DRIVEBOUND_RECOVERY_PUBLISH_METRIC")),
        help="Publish the aggregate structural-evidence outcome to metrics Redis",
    )
    return result


def command(args: argparse.Namespace) -> list[str]:
    if args.manifest is None or args.report_dir is None:
        raise ValueError("manifest and report directory are required by argument or environment")
    verifier = Path(__file__).resolve().with_name("recovery_drill.py")
    result = [
        sys.executable,
        str(verifier),
        str(args.manifest),
        "--report-dir",
        str(args.report_dir),
    ]
    if args.publish_metric:
        result.append("--publish-metric")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        invocation = command(args)
    except ValueError as exc:
        print(f"recovery evidence error: {exc}", file=sys.stderr)
        return 2
    # No shell expansion: paths remain exact operator-supplied arguments.
    try:
        return subprocess.run(invocation, check=False).returncode
    except OSError:
        # Process-launch exceptions commonly include the executable and host
        # paths. Keep centralized scheduler logs path-free.
        print("recovery evidence error: verifier process could not be started", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
