#!/usr/bin/env python3
"""Resumably upload a local media folder to Drivebound without altering source files."""

import argparse
import hashlib
import json
import mimetypes
import sys
import urllib.error
import urllib.request
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(url: str, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    merged = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read() or b"{}")


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def sync_file(path: Path, server: str, token: str, state: dict, state_path: Path, chunk_size: int) -> str:
    auth_headers = {"Authorization": f"Bearer {token}"}
    key = str(path.resolve())
    stat = path.stat()
    fingerprint = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    saved = state.get(key, {})
    checksum = saved.get("checksum") if saved.get("fingerprint") == fingerprint else None
    if not checksum:
        checksum = sha256(path)

    upload = None
    if saved.get("fingerprint") == fingerprint and saved.get("upload_id"):
        upload_id = saved["upload_id"]
        try:
            head = urllib.request.Request(f"{server}/api/v1/uploads/{upload_id}", method="HEAD", headers=auth_headers)
            with urllib.request.urlopen(head, timeout=30) as response:
                upload = {
                    "id": upload_id,
                    "upload_url": f"/api/v1/uploads/{upload_id}",
                    "offset": int(response.headers["Upload-Offset"]),
                    "total_size": stat.st_size,
                    "status": response.headers.get("Upload-Status", "active"),
                }
        except urllib.error.HTTPError:
            upload = None

    if upload is None:
        upload = request_json(
            f"{server}/api/v1/uploads",
            method="POST",
            payload={
                "filename": path.name,
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "total_size": stat.st_size,
                "checksum": checksum,
            },
            headers=auth_headers,
        )
        if upload.get("duplicate"):
            state[key] = {"fingerprint": fingerprint, "checksum": checksum, "complete": True}
            save_state(state_path, state)
            return "duplicate"

    offset = int(upload["offset"])
    state[key] = {"fingerprint": fingerprint, "checksum": checksum, "upload_id": upload["id"]}
    save_state(state_path, state)
    with path.open("rb") as source:
        source.seek(offset)
        while offset < stat.st_size:
            chunk = source.read(min(chunk_size, stat.st_size - offset))
            endpoint = f"{server}{upload['upload_url']}"
            request = urllib.request.Request(
                endpoint,
                data=chunk,
                method="PATCH",
                headers={**auth_headers, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                result = json.loads(response.read())
            offset = int(result["offset"])
            state[key]["offset"] = offset
            save_state(state_path, state)

    state[key] = {"fingerprint": fingerprint, "checksum": checksum, "complete": True}
    save_state(state_path, state)
    return "uploaded"


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumably sync a media folder to Drivebound")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--token", required=True, help="Access token from POST /api/v1/auth/token")
    parser.add_argument("--chunk-size", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--state-file", type=Path)
    arguments = parser.parse_args()
    folder = arguments.folder.resolve()
    if not folder.is_dir():
        parser.error(f"Folder does not exist: {folder}")
    state_path = arguments.state_file or folder / ".drivebound-sync.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS)
    print(f"Drivebound: {len(files)} media files found in {folder}")
    for index, path in enumerate(files, 1):
        try:
            result = sync_file(path, arguments.server.rstrip("/"), arguments.token, state, state_path, arguments.chunk_size)
            print(f"[{index}/{len(files)}] {result}: {path.relative_to(folder)}")
        except (OSError, urllib.error.URLError, ValueError) as exc:
            print(f"[{index}/{len(files)}] failed: {path.relative_to(folder)} ({exc})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
