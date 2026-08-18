import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.recovery_drill import RecoveryDrillManifestError, load_recovery_drill_manifest, run_recovery_drill


def load_tool_module(filename: str):
    path = Path(__file__).resolve().parents[2] / "tools" / filename
    spec = importlib.util.spec_from_file_location(f"drivebound_{path.stem}_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cli_module():
    return load_tool_module("recovery_drill.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(tmp_path: Path, *, database_hash: str | None = None) -> Path:
    database = tmp_path / "database.dump"
    configuration = tmp_path / "configuration.json"
    media = tmp_path / "sample.enc"
    database.write_bytes(b"logical archive fixture")
    configuration.write_text(
        json.dumps({"schema": 1, "configuration": {"deployment_mode": "staging"}}),
        encoding="utf-8",
    )
    media.write_bytes(b"encrypted media fixture")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 1,
                "evidence_id": "monthly-2026-08",
                "release": "v0.5.0",
                "database": {"path": database.name, "sha256": database_hash or digest(database)},
                "configuration": {"path": configuration.name, "sha256": digest(configuration)},
                "media_samples": [{"path": media.name, "sha256": digest(media)}],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_verify_only_recovery_drill_returns_redacted_report_and_preserves_inputs(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path)
    loaded = load_recovery_drill_manifest(manifest_path)
    before = {item.path: (item.path.read_bytes(), item.path.stat().st_mtime_ns) for item in loaded.inputs}

    _manifest, report = run_recovery_drill(
        manifest_path,
        database_verifier=lambda _path: "archive structure is readable",
    )

    assert report["overall"] == "passed"
    assert report["mode"] == "verify_only"
    assert report["evidence_scope"] == "structural_recovery_inputs_only"
    assert report["safety"] == {
        "database_restore_attempted": False,
        "isolated_restore_attempted": False,
        "application_recovery_proven": False,
        "media_writes_attempted": False,
        "paths_redacted": True,
    }
    assert report["summary"] == {"passed": 3, "failed": 0, "total": 3}
    encoded = json.dumps(report)
    assert str(tmp_path) not in encoded
    assert all(item.expected_sha256 not in encoded for item in loaded.inputs)
    assert all(item.path.name not in encoded for item in loaded.inputs)
    for item in loaded.inputs:
        content, modified = before[item.path]
        assert item.path.read_bytes() == content
        assert item.path.stat().st_mtime_ns == modified


def test_checksum_failure_does_not_run_archive_parser(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, database_hash="0" * 64)
    parser_called = False

    def parser(_path: Path) -> str:
        nonlocal parser_called
        parser_called = True
        return "unexpected"

    _manifest, report = run_recovery_drill(manifest_path, database_verifier=parser)

    database = next(check for check in report["checks"] if check["kind"] == "database")
    assert report["overall"] == "failed"
    assert database["failure_code"] == "checksum_mismatch"
    assert parser_called is False


def test_manifest_rejects_duplicate_inputs_and_unknown_fields(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["media_samples"][0] = document["database"]
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RecoveryDrillManifestError, match="unique"):
        load_recovery_drill_manifest(manifest_path)

    document["media_samples"][0] = {"path": "sample.enc", "sha256": digest(tmp_path / "sample.enc")}
    document["unexpected"] = "not accepted"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RecoveryDrillManifestError, match="exactly"):
        load_recovery_drill_manifest(manifest_path)


def test_manifest_requires_at_least_one_media_recovery_sample(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["media_samples"] = []
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RecoveryDrillManifestError, match="between 1 and 25"):
        load_recovery_drill_manifest(manifest_path)


def test_report_writer_never_overwrites_an_input_or_existing_report(tmp_path: Path) -> None:
    cli = load_cli_module()
    protected = tmp_path / "database.dump"
    protected.write_bytes(b"archive")
    with pytest.raises(RecoveryDrillManifestError, match="cannot replace"):
        cli._write_new_report(protected, b"{}\n", {protected.resolve()})

    report = tmp_path / "report.json"
    report.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cli._write_new_report(report, b"replacement", set())
    assert report.read_text(encoding="utf-8") == "existing"


def test_explicit_metric_publish_failure_is_reported_without_losing_report(tmp_path: Path, monkeypatch) -> None:
    cli = load_cli_module()
    manifest_path = write_manifest(tmp_path)
    manifest = load_recovery_drill_manifest(manifest_path)
    report = {"overall": "passed", "mode": "verify_only"}
    monkeypatch.setattr(cli, "run_recovery_drill", lambda _path: (manifest, report))
    monkeypatch.setattr(cli.metrics.operations, "observe_recovery", lambda *_args: False)
    output = tmp_path / "report.json"

    status = cli.main([str(manifest_path), "--report", str(output), "--publish-metric"])

    assert status == 2
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_report_directory_creates_exclusive_timestamped_evidence(tmp_path: Path, monkeypatch) -> None:
    cli = load_cli_module()
    manifest_path = write_manifest(tmp_path)
    manifest = load_recovery_drill_manifest(manifest_path)
    report_dir = tmp_path / "retained-evidence"
    report_dir.mkdir()
    base = {
        "overall": "passed",
        "mode": "verify_only",
        "evidence_scope": "structural_recovery_inputs_only",
        "evidence_id": manifest.evidence_id,
    }
    generated = datetime(2026, 8, 15, 17, 30, 1, 123456, tzinfo=timezone.utc)
    reports = iter((
        {**base, "generated_at": generated.isoformat()},
        {**base, "generated_at": (generated + timedelta(microseconds=1)).isoformat()},
    ))
    monkeypatch.setattr(cli, "run_recovery_drill", lambda _path: (manifest, next(reports)))

    assert cli.main([str(manifest_path), "--report-dir", str(report_dir)]) == 0
    assert cli.main([str(manifest_path), "--report-dir", str(report_dir)]) == 0

    retained = sorted(report_dir.iterdir())
    assert len(retained) == 2
    assert all(re.fullmatch(r"monthly-2026-08-20260815T173001\d{6}Z\.json", path.name) for path in retained)
    assert all(path.read_text(encoding="utf-8") for path in retained)


FAILURE_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "recovery_evidence_failure_cases.json"
FAILURE_CASES = json.loads(FAILURE_FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", FAILURE_CASES, ids=[case["name"] for case in FAILURE_CASES])
def test_bounded_failure_injection_produces_redacted_stable_codes(tmp_path: Path, case: dict[str, str]) -> None:
    allowed = {
        "checksum_mismatch",
        "database_invalid_format",
        "database_tool_unavailable",
        "input_changed_during_read",
        "redacted_unexpected_failure",
    }
    assert len(FAILURE_CASES) <= 8
    assert case["name"] in allowed
    manifest_path = write_manifest(
        tmp_path,
        database_hash="0" * 64 if case["name"] == "checksum_mismatch" else None,
    )
    database_path = tmp_path / "database.dump"
    secret_marker = f"secret-host-path={tmp_path};digest={digest(database_path)}"

    def verifier(_path: Path) -> str:
        if case["name"] == "database_invalid_format":
            raise ValueError(secret_marker)
        if case["name"] == "database_tool_unavailable":
            raise RuntimeError(secret_marker)
        if case["name"] == "input_changed_during_read":
            _path.write_bytes(_path.read_bytes() + b"changed-by-test-injector")
            return "changed"
        if case["name"] == "redacted_unexpected_failure":
            raise Exception(secret_marker)
        return "not called for checksum mismatch"

    _manifest, report = run_recovery_drill(manifest_path, database_verifier=verifier)
    database = next(check for check in report["checks"] if check["kind"] == "database")
    encoded = json.dumps(report)

    assert report["overall"] == "failed"
    assert database["failure_code"] == case["expected_code"]
    assert secret_marker not in encoded
    assert str(tmp_path) not in encoded


def test_scheduler_wrapper_builds_a_shell_free_retained_evidence_command(tmp_path: Path, monkeypatch) -> None:
    wrapper = load_tool_module("run_recovery_evidence.py")
    manifest = tmp_path / "input with spaces" / "manifest.json"
    report_dir = tmp_path / "retained reports"
    captured: list[list[str]] = []
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda command, check: captured.append(command) or SimpleNamespace(returncode=1),
    )

    status = wrapper.main(["--manifest", str(manifest), "--report-dir", str(report_dir), "--publish-metric"])

    assert status == 1
    assert captured == [[
        sys.executable,
        str((Path(__file__).resolve().parents[2] / "tools" / "recovery_drill.py").resolve()),
        str(manifest),
        "--report-dir",
        str(report_dir),
        "--publish-metric",
    ]]


def test_cli_filesystem_errors_do_not_leak_host_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    cli = load_cli_module()
    marker = str(tmp_path / "private-host-location")
    monkeypatch.setattr(cli, "run_recovery_drill", lambda _path: (_ for _ in ()).throw(FileNotFoundError(marker)))

    assert cli.main([str(tmp_path / "manifest.json")]) == 2

    error = capsys.readouterr().err
    assert marker not in error
    assert "filesystem operation failed" in error


def test_scheduler_launch_errors_do_not_leak_host_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    wrapper = load_tool_module("run_recovery_evidence.py")
    marker = str(tmp_path / "private-python-or-verifier-path")
    monkeypatch.setattr(
        wrapper.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(marker)),
    )

    status = wrapper.main([
        "--manifest",
        str(tmp_path / "manifest.json"),
        "--report-dir",
        str(tmp_path / "reports"),
    ])

    assert status == 2
    error = capsys.readouterr().err
    assert marker not in error
    assert "verifier process could not be started" in error
