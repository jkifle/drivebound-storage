#!/usr/bin/env python3
"""Local-only acceptance client for test-pilot-stack.ps1's disposable stack.

This is deliberately not a client for an operator's real installation. It never
accepts credentials, arbitrary server addresses, or existing photo folders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from PIL import Image

REPOSITORY = Path(__file__).resolve().parent.parent
OWNER_EMAIL = "pilot-owner@example.com"
CAPTURE_TIME = "2024-05-06T07:08:09"
FILE_TIME = "2024-05-07T08:09:10Z"
NOT_TESTED = [
    "real SMTP or Google sign-in", "physical passkeys", "cellular or Tailscale access",
    "physical disk independence or unplugging", "Windows reboot", "video playback",
    "signed mobile apps", "full isolated PostgreSQL disaster recovery",
    "live broker/worker interruption",
]


class PilotFailure(Exception):
    """Only fixed, non-sensitive diagnostic codes belong in shared reports."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PilotFailure(code)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def pilot_root(path: Path) -> Path:
    # Inspect the lexical path before resolving it. Resolving both a junctioned
    # pilot-runs parent and the allowed path would otherwise hide the escape.
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not (getattr(info, "st_file_attributes", 0)
                         & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)),
                "run_directory_is_link")
    root = path.resolve()
    allowed = (REPOSITORY / ".drivebound" / "pilot-runs").resolve()
    require(root.parent == allowed and re.fullmatch(r"[0-9a-f]{32}", root.name) is not None,
            "run_directory_not_disposable")
    return root


def local_origin(value: str, hostname: str) -> int:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise PilotFailure("invalid_test_port") from None
    require(parsed.scheme == "http" and parsed.hostname == hostname
            and parsed.username is None and parsed.password is None
            and parsed.path == "" and not parsed.query and not parsed.fragment
            and port is not None and 10000 <= port <= 65535, "nonlocal_test_origin")
    return port


def load_run(path: Path) -> dict:
    root = pilot_root(path)
    config = json.loads((root / "pilot-run.json").read_text(encoding="utf-8-sig"))
    require(config.get("schema") == 1, "unsupported_run_schema")
    require(config.get("project_name") == f"drivebound-pilot-{root.name}", "project_identity_mismatch")
    require(config.get("owner_email") == OWNER_EMAIL, "nonfixture_owner")
    api_port = local_origin(config["api_url"], "127.0.0.1")
    app_port = local_origin(config["app_url"], "localhost")
    require(api_port != app_port, "test_port_collision")
    for key, target in (("imports_path", root / "imports"), ("report_path", root / "report.json")):
        require(Path(config[key]).resolve() == target, "fixture_path_escape")
    installation = json.loads((root / ".drivebound" / "installation.json").read_text(encoding="utf-8-sig"))
    require(installation["compose_project_name"] == config["project_name"]
            and installation["owner_email"] == OWNER_EMAIL
            and Path(installation["paths"]["imports"]).resolve() == root / "imports",
            "installation_identity_mismatch")
    config["root"] = root
    return config


def photo(path: Path, color: str) -> None:
    exif = Image.Exif()
    exif[271], exif[272] = "Drivebound fixture", "Pilot camera"
    exif[36867] = "2024:05:06 07:08:09"
    exif[34853] = {1: "N", 2: (37.0, 45.0, 0.0), 3: "W", 4: (122.0, 25.0, 0.0)}
    with path.open("xb") as target:
        Image.new("RGB", (48, 32), color).save(target, format="JPEG", exif=exif)


def prepare_fixtures(imports: Path) -> None:
    root = pilot_root(imports.parent)
    require(imports.resolve() == root / "imports", "fixture_path_escape")
    require(imports.is_dir() and not any(imports.iterdir()), "imports_not_empty")
    require(not (root / "fixtures.json").exists() and not (root / "upload.jpg").exists(),
            "fixtures_already_prepared")
    nested = imports / "nested photos"
    nested.mkdir()
    original = nested / "旅行 photo.jpg"
    photo(original, "navy")
    with (imports / "identical copy.jpg").open("xb") as target:
        target.write(original.read_bytes())
    with (imports / "no location.png").open("xb") as target:
        Image.new("RGB", (30, 20), "green").save(target, format="PNG")
    photo(root / "upload.jpg", "orange")  # Distinct bytes must exercise managed encryption.
    records = [{"relative_path": file.relative_to(imports).as_posix(), "sha256": digest(file),
                "size": file.stat().st_size, "mtime_ns": file.stat().st_mtime_ns}
               for file in sorted(imports.rglob("*")) if file.is_file()]
    with (root / "fixtures.json").open("x", encoding="utf-8") as output:
        json.dump({"schema": 1, "imports": records, "upload_sha256": digest(root / "upload.jpg")}, output, indent=2)


class Acceptance:
    def __init__(self, config: dict, client: httpx.Client, *, timeout: float = 120):
        self.config, self.client, self.timeout = config, client, timeout
        self.root = config["root"]
        self.fixtures = json.loads((self.root / "fixtures.json").read_text(encoding="utf-8"))
        self.checks: list[dict] = []
        self.token: str | None = None

    def request(self, method: str, route: str, expected=200, **kwargs) -> httpx.Response:
        headers = {"Origin": self.config["app_url"]}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, self.config["api_url"] + "/api/v1" + route,
                                       headers=headers, **kwargs)
        allowed = (expected,) if isinstance(expected, int) else expected
        require(response.status_code in allowed, f"unexpected_http_status_{response.status_code}")
        return response

    def check(self, name, action):
        try:
            result = action()
        except Exception:
            self.checks.append({"check": name, "status": "failed"})
            raise
        self.checks.append({"check": name, "status": "passed"})
        print(f"PASS {name}", flush=True)
        return result

    def wait_for(self, callback, predicate, code):
        deadline = time.monotonic() + self.timeout
        while True:
            value = callback()
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                raise PilotFailure(code)
            time.sleep(1)

    def register(self, email):
        self.token = None
        self.client.cookies.clear()
        password = secrets.token_urlsafe(32)
        result = self.request("POST", "/auth/register", 201, json={
            "email": email, "password": password, "display_name": "Disposable pilot account",
        }).json()
        verification = urlsplit(result.get("development_verification_url") or "")
        # Never navigate to a server-supplied URL; only consume a local fixture token.
        require(f"{verification.scheme}://{verification.netloc}" == self.config["app_url"]
                and verification.path == "/verify-email", "local_verification_unavailable")
        token = parse_qs(verification.fragment).get("token", [None])[0]
        require(isinstance(token, str), "verification_token_missing")
        self.request("POST", "/auth/verify-email", json={"token": token})
        login = self.request("POST", "/auth/login", json={"email": email, "password": password}).json()
        self.token = login["access_token"]
        require(bool(login["user"]["email_verified_at"]), "account_not_verified")
        return self.token

    def sources_unchanged(self):
        imports = self.root / "imports"
        actual = {path.relative_to(imports).as_posix() for path in imports.rglob("*") if path.is_file()}
        require(actual == {item["relative_path"] for item in self.fixtures["imports"]}, "source_file_set_changed")
        for item in self.fixtures["imports"]:
            path = imports / item["relative_path"]
            require(digest(path) == item["sha256"] and path.stat().st_mtime_ns == item["mtime_ns"],
                    "source_bytes_or_timestamp_changed")

    def detail(self, identifier):
        return self.wait_for(lambda: self.request("GET", f"/assets/{identifier}").json(),
                             lambda item: item["processing_status"] == "complete", "asset_processing_timeout")

    def metadata(self, item, *, located=True):
        if located:
            require(item["camera_make"] == "Drivebound fixture" and item["camera_model"] == "Pilot camera",
                    "camera_metadata_changed")
            require((item["taken_at"] or "").startswith(CAPTURE_TIME), "capture_time_changed")
            require(abs((item["latitude"] or 0) - 37.75) < 0.00001
                    and abs((item["longitude"] or 0) + 122.4166666667) < 0.00001, "gps_changed")
        else:
            require(item["latitude"] is None and item["longitude"] is None, "invented_location")

    def original(self, item):
        response = self.request("GET", f"/assets/{item['id']}/original")
        require(hashlib.sha256(response.content).hexdigest() == item["checksum"], "download_bytes_changed")

    def scan(self, library):
        self.request("POST", f"/libraries/{library}/scan", 202)
        self.wait_for(lambda: self.request("GET", "/libraries").json(),
                      lambda items: any(item["id"] == library and item["status"] == "idle" for item in items),
                      "library_scan_timeout")

    def imported(self):
        setup = self.request("GET", "/libraries/setup").json()
        require(setup["can_connect"] and setup["storage_available"], "owner_cannot_connect")
        library = self.request("POST", "/libraries/connect", 201, json={"name": "Disposable photos"}).json()["id"]
        again = self.request("POST", "/libraries/connect", 201, json={"name": "Disposable photos"}).json()["id"]
        require(library == again, "duplicate_library_created")
        self.scan(library)
        items = self.request("GET", "/assets?limit=200").json()["items"]
        require(len(items) == 2, "import_deduplication_failed")
        details = [self.detail(item["id"]) for item in items]
        require({item["checksum"] for item in details} == {item["sha256"] for item in self.fixtures["imports"]},
                "import_checksum_set_mismatch")
        for item in details:
            self.metadata(item, located=item["mime_type"] == "image/jpeg")
            self.original(item)
        self.scan(library)
        require({item["id"] for item in self.request("GET", "/assets?limit=200").json()["items"]}
                == {item["id"] for item in items}, "rescan_changed_catalog")
        self.request("POST", "/auth/onboarding", json={"display_name": "Pilot owner"})
        return next(item for item in details if item["mime_type"] == "image/jpeg")

    def upload(self):
        content = (self.root / "upload.jpg").read_bytes()
        payload = {"filename": "旅行 uploaded photo.jpg", "mime_type": "image/jpeg", "total_size": len(content),
                   "checksum": self.fixtures["upload_sha256"], "file_modified_at": FILE_TIME,
                   "file_created_at": FILE_TIME}
        pending = self.request("POST", "/uploads", 201, json=payload).json()
        require(not pending["duplicate"], "upload_fixture_was_duplicate")
        route = f"/uploads/{pending['id']}"
        middle = len(content) // 2
        self.request("PATCH", route, content=content[:middle], headers={"Upload-Offset": "0"})
        status = self.request("HEAD", route, 204)
        require(int(status.headers["Upload-Offset"]) == middle, "resume_offset_mismatch")
        self.request("PATCH", route, 409, content=b"wrong offset", headers={"Upload-Offset": "0"})
        completed = self.request("PATCH", route, content=content[middle:], headers={"Upload-Offset": str(middle)}).json()
        item = self.detail(completed["asset"]["id"])
        require(item["storage_source"] == "managed" and item["encryption_version"] > 0, "upload_not_encrypted")
        require(item["original_filename"] == payload["filename"], "upload_filename_changed")
        expected_date = datetime.fromisoformat(FILE_TIME.replace("Z", "+00:00"))
        for key in ("file_created_at", "file_modified_at"):
            require(datetime.fromisoformat(item[key].replace("Z", "+00:00")) == expected_date, "file_time_changed")
        self.metadata(item)
        self.original(item)
        duplicate = self.request("POST", "/uploads", 201, json=payload).json()
        require(duplicate["duplicate"] and duplicate["asset"]["id"] == item["id"], "upload_deduplication_failed")
        return item

    def isolation(self, imported, uploaded):
        owner = self.token
        self.register("pilot-other@example.com")
        require(not self.request("GET", "/assets").json()["items"], "other_account_sees_assets")
        require(not self.request("GET", "/libraries/setup").json()["can_connect"], "other_account_has_host_grant")
        self.request("POST", "/libraries/connect", 403, json={"name": "Unauthorized"})
        self.request("POST", "/storage/backups/run", 403)
        for item in (imported, uploaded):
            self.request("GET", f"/assets/{item['id']}", 404)
            self.request("GET", f"/assets/{item['id']}/original", 404)
        self.client.cookies.clear()
        self.token = owner

    def restore_copy(self, item):
        route = f"/assets/{item['id']}"
        self.request("POST", route + "/protect", 202)
        self.wait_for(lambda: self.request("GET", route).json(),
                      lambda result: result["protection_status"] == "protected", "protection_timeout")
        self.request("POST", route + "/restore", 202)
        restored = self.wait_for(lambda: self.request("GET", route).json(),
                                 lambda result: result["restore_status"] == "restored", "restore_timeout")
        require(restored["storage_source"] == "managed", "restore_did_not_create_managed_copy")
        self.metadata(restored)
        self.original(restored)

    def backups(self):
        self.request("POST", "/storage/backups/run", (202, 409))
        self.wait_for(lambda: self.request("GET", "/storage/backups").json(),
                      lambda items: {"database", "configuration"}.issubset({item["kind"] for item in items
                                   if item["status"] == "created" and item["verification_status"] == "verified"}),
                      "backup_verification_timeout")
        require(self.request("GET", "/storage/recovery").json()["recoverable"], "archive_evidence_not_current")

    def run(self):
        self.check("source_fixture_baseline", self.sources_unchanged)
        self.check("api_readiness", lambda: require(self.request("GET", "/ready").json()["status"] == "ready", "api_not_ready"))
        self.check("frontend_http", lambda: require(self.client.get(self.config["app_url"]).status_code == 200, "frontend_not_ready"))
        self.check("anonymous_asset_denial", lambda: self.request("GET", "/assets", 401))
        self.check("local_owner_registration_verification_login", lambda: self.register(OWNER_EMAIL))
        imported = self.check("owner_import_metadata_download_rescan", self.imported)
        uploaded = self.check("resumable_encrypted_upload_metadata_download", self.upload)
        self.check("second_account_ownership_denial", lambda: self.isolation(imported, uploaded))
        self.check("external_original_restored_to_managed_copy", lambda: self.restore_copy(imported))
        self.check("postgres_configuration_archive_verification", self.backups)
        self.check("source_bytes_and_timestamps_unchanged", self.sources_unchanged)


def execute(config: dict) -> int:
    report_path = Path(config["report_path"])
    require(not report_path.exists(), "acceptance_report_already_exists")
    report = {"schema": 1, "run_id": config["root"].name, "recorded_at": datetime.now(timezone.utc).isoformat(),
              "status": "failed", "checks": [], "not_tested": NOT_TESTED}
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            acceptance = Acceptance(config, client)
            report["checks"] = acceptance.checks
            acceptance.run()
        report["status"] = "passed"
    except Exception as exc:
        # No response body, URL token, password, host path, or raw exception is
        # copied into retained evidence. Private container logs stay separate.
        report["failure_code"] = str(exc) if isinstance(exc, PilotFailure) else type(exc).__name__
    with report_path.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
    print(f"Local acceptance {report['status']}; {len(report['checks'])} checks recorded. Remote/hardware gates remain open.")
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-fixtures", type=Path)
    action.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.prepare_fixtures:
            prepare_fixtures(args.prepare_fixtures)
            print("Disposable photo fixtures prepared. No services contacted.")
            return 0
        return execute(load_run(args.run_dir))
    except Exception as exc:
        code = str(exc) if isinstance(exc, PilotFailure) else type(exc).__name__
        print(f"Pilot preflight failed: {code}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
