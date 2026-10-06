"""Acceptance-runner contracts. Mock transport is not live-stack evidence."""

import importlib.util
import json
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

spec = importlib.util.spec_from_file_location("pilot_acceptance", Path(__file__).resolve().parents[2] / "tools/pilot_acceptance.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "REPOSITORY", tmp_path)
    root = tmp_path / ".drivebound" / "pilot-runs" / uuid.uuid4().hex
    imports = root / "imports"
    imports.mkdir(parents=True)
    pilot.prepare_fixtures(imports)
    config = {"schema": 1, "project_name": f"drivebound-pilot-{root.name}",
              "owner_email": pilot.OWNER_EMAIL, "api_url": "http://127.0.0.1:18000",
              "app_url": "http://localhost:13000", "imports_path": str(imports),
              "report_path": str(root / "report.json")}
    (root / "pilot-run.json").write_text(json.dumps(config))
    (root / ".drivebound").mkdir()
    (root / ".drivebound/installation.json").write_text(json.dumps({
        "compose_project_name": config["project_name"], "owner_email": pilot.OWNER_EMAIL,
        "paths": {"imports": str(imports)},
    }))
    return pilot.load_run(root)


def test_fixture_metadata_and_dedup_identity(prepared):
    root = prepared["root"]
    fixture = json.loads((root / "fixtures.json").read_text())
    assert len(fixture["imports"]) == 3
    hashes = {item["sha256"] for item in fixture["imports"]}
    assert len(hashes) == 2 and fixture["upload_sha256"] not in hashes
    with Image.open(root / "upload.jpg") as image:
        exif = image.getexif()
        assert exif[271] == "Drivebound fixture"
        assert exif[36867] == "2024:05:06 07:08:09"
        assert exif.get_ifd(34853)[1] == "N"
    with pytest.raises(pilot.PilotFailure, match="imports_not_empty"):
        pilot.prepare_fixtures(root / "imports")


@pytest.mark.parametrize("url", ["https://example.com:18000", "http://localhost:8000", "http://user@127.0.0.1:18000",
                                  "http://127.0.0.1:18000/path", "http://127.0.0.1:18000?token=secret"])
def test_non_disposable_origins_are_rejected(url):
    with pytest.raises(pilot.PilotFailure):
        pilot.local_origin(url, "127.0.0.1")


def test_run_cannot_target_real_installation(prepared):
    with pytest.raises(pilot.PilotFailure, match="run_directory_not_disposable"):
        pilot.load_run(pilot.REPOSITORY)
    root = prepared["root"]
    saved = json.loads((root / "pilot-run.json").read_text())
    saved["report_path"] = str(pilot.REPOSITORY / "report.json")
    (root / "pilot-run.json").write_text(json.dumps(saved))
    with pytest.raises(pilot.PilotFailure, match="fixture_path_escape"):
        pilot.load_run(root)


@pytest.mark.parametrize("parent_level", [0, 1, 2])
@pytest.mark.parametrize("link_kind", ["symlink", "reparse"])
def test_run_rejects_linked_ancestors_before_resolving(prepared, monkeypatch, parent_level, link_kind):
    root = prepared["root"]
    linked = root if parent_level == 0 else root.parents[parent_level - 1]
    original_lstat = Path.lstat

    def mocked_lstat(path):
        if path == linked:
            return SimpleNamespace(st_mode=stat.S_IFLNK if link_kind == "symlink" else stat.S_IFDIR,
                                   st_file_attributes=0x400 if link_kind == "reparse" else 0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", mocked_lstat)
    with pytest.raises(pilot.PilotFailure, match="run_directory_is_link"):
        pilot.load_run(root)
    with pytest.raises(pilot.PilotFailure, match="run_directory_is_link"):
        pilot.prepare_fixtures(root / "imports")


def test_report_redacts_exception_and_cannot_be_overwritten(prepared, monkeypatch):
    def fail(self):
        raise RuntimeError("password=do-not-retain http://private/token=secret")
    monkeypatch.setattr(pilot.Acceptance, "run", fail)
    assert pilot.execute(prepared) == 1
    report = json.loads(Path(prepared["report_path"]).read_text())
    assert report["failure_code"] == "RuntimeError" and report["status"] == "failed"
    assert "do-not-retain" not in json.dumps(report) and report["not_tested"]
    with pytest.raises(pilot.PilotFailure, match="acceptance_report_already_exists"):
        pilot.execute(prepared)


def test_source_mutation_is_detected(prepared):
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("No requests expected"))) as client:
        acceptance = pilot.Acceptance(prepared, client)
        acceptance.sources_unchanged()
        (prepared["root"] / "imports/identical copy.jpg").write_bytes(b"changed")
        with pytest.raises(pilot.PilotFailure, match="source_bytes_or_timestamp_changed"):
            acceptance.sources_unchanged()


def test_complete_client_protocol_with_mock_transport(prepared):
    root = prepared["root"]
    fixtures = json.loads((root / "fixtures.json").read_text())
    source_files = sorted((root / "imports").rglob("*.jpg"))
    source_bytes = source_files[0].read_bytes()
    plain_bytes = (root / "imports/no location.png").read_bytes()
    upload_bytes = (root / "upload.jpg").read_bytes()
    items = {}
    state = {"uploaded": False, "imported": False, "email": ""}

    def asset(identifier, content, *, located=True):
        return {"id": identifier, "processing_status": "complete", "storage_source": "external",
                "encryption_version": 0, "checksum": pilot.hashlib.sha256(content).hexdigest(),
                "mime_type": "image/jpeg" if located else "image/png", "camera_make": "Drivebound fixture",
                "camera_model": "Pilot camera", "taken_at": pilot.CAPTURE_TIME + "+00:00",
                "latitude": 37.75 if located else None, "longitude": -122.4166666667 if located else None,
                "protection_status": "protected", "restore_status": "not_requested"}

    items["jpeg"] = asset("jpeg", source_bytes)
    items["png"] = asset("png", plain_bytes, located=False)
    items["uploaded"] = asset("uploaded", upload_bytes)
    items["uploaded"].update({"storage_source": "managed", "encryption_version": 1,
                              "original_filename": "旅行 uploaded photo.jpg",
                              "file_created_at": pilot.FILE_TIME, "file_modified_at": pilot.FILE_TIME})

    def handler(request):
        path = request.url.path.removeprefix("/api/v1")
        method = request.method
        other = request.headers.get("Authorization") == "Bearer other-token"
        payload = json.loads(request.content) if request.content and request.headers.get("Content-Type") == "application/json" else {}
        if path == "/": return httpx.Response(200, text="Fixture frontend")
        if path == "/ready": return httpx.Response(200, json={"status": "ready"})
        if path == "/auth/register":
            state["email"] = payload["email"]
            return httpx.Response(201, json={"development_verification_url": prepared["app_url"] + "/verify-email#token=" + "a" * 64})
        if path in ("/auth/verify-email", "/auth/onboarding"): return httpx.Response(200, json={})
        if path == "/auth/login":
            return httpx.Response(200, json={"access_token": "owner-token" if state["email"] == pilot.OWNER_EMAIL else "other-token",
                                            "user": {"email_verified_at": pilot.FILE_TIME}})
        if path == "/libraries/setup": return httpx.Response(200, json={"can_connect": not other, "storage_available": True})
        if path == "/libraries/connect": return httpx.Response(403 if other else 201, json={"id": "library"})
        if path == "/libraries/library/scan":
            state["imported"] = True
            return httpx.Response(202, json={})
        if path == "/libraries": return httpx.Response(200, json=[{"id": "library", "status": "idle"}])
        if path == "/assets":
            if not request.headers.get("Authorization"): return httpx.Response(401)
            return httpx.Response(200, json={"items": [] if other else [items["jpeg"], items["png"]]})
        if path == "/uploads":
            return httpx.Response(201, json={"id": "upload", "duplicate": state["uploaded"], "asset": items["uploaded"]})
        if path == "/uploads/upload":
            offset = request.headers.get("Upload-Offset")
            if method == "HEAD": return httpx.Response(204, headers={"Upload-Offset": str(len(upload_bytes) // 2)})
            if request.content == b"wrong offset": return httpx.Response(409)
            if offset != "0": state["uploaded"] = True
            return httpx.Response(200, json={"asset": items["uploaded"]})
        if path.startswith("/assets/"):
            identifier = path.split("/")[2]
            if other: return httpx.Response(404)
            if path.endswith("/original"):
                return httpx.Response(200, content={"jpeg": source_bytes, "png": plain_bytes, "uploaded": upload_bytes}[identifier])
            if path.endswith("/restore"):
                items[identifier].update({"storage_source": "managed", "restore_status": "restored"})
            return httpx.Response(202 if method == "POST" else 200, json=items[identifier])
        if path == "/storage/backups/run": return httpx.Response(403 if other else 202)
        if path == "/storage/backups": return httpx.Response(200, json=[{"kind": kind, "status": "created", "verification_status": "verified"}
                                                                       for kind in ("database", "configuration")])
        if path == "/storage/recovery": return httpx.Response(200, json={"recoverable": True})
        pytest.fail(f"Unexpected mock route: {method} {path}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        acceptance = pilot.Acceptance(prepared, client, timeout=0)
        acceptance.run()
    assert len(acceptance.checks) == 11 and all(check["status"] == "passed" for check in acceptance.checks)
    assert state["imported"] and state["uploaded"]
