#!/usr/bin/env python3
"""Selective, journal-based desktop-folder sync for Drivebound.

The state file is local to a selected folder. It records stable logical IDs and
acknowledged revisions, so a rename is a move and a missing path is an explicit
delete operation—not an inference the server makes from a scan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

STATE_NAME = ".drivebound-sync.json"
SCHEMA = 2


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(path: Path, checksum: str | None = None) -> dict[str, int | str]:
    stat = path.stat()
    value: dict[str, int | str] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    if checksum:
        value["checksum"] = checksum
    return value


def request_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    merged = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def state_template() -> dict[str, Any]:
    return {"schema": SCHEMA, "client_id": str(uuid.uuid4()), "root_id": None, "cursor": 0, "entries": {}, "conflicts": []}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return state_template()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read sync state: {exc}") from exc
    if state.get("schema") != SCHEMA:
        raise RuntimeError("This folder has an incompatible Drivebound sync state; use a new --state-file after preserving the old one.")
    state.setdefault("entries", {})
    state.setdefault("conflicts", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def safe_relative(root: Path, path: Path) -> str:
    value = path.resolve().relative_to(root.resolve()).as_posix()
    if value == STATE_NAME or value.startswith(".drivebound-"):
        raise ValueError("Drivebound internal path")
    return value


def scan(folder: Path, state_path: Path) -> dict[str, Path]:
    results: dict[str, Path] = {}
    for path in folder.rglob("*"):
        if not path.is_file() or path == state_path or path.name.startswith(".drivebound-"):
            continue
        try:
            results[safe_relative(folder, path)] = path
        except (OSError, ValueError):
            continue
    return results


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def resume_or_create(path: Path, server: str, token: str, entry: dict[str, Any], state: dict[str, Any], state_path: Path, chunk_size: int) -> dict[str, Any]:
    headers = auth(token)
    current = fingerprint(path)
    upload: dict[str, Any] | None = None
    if entry.get("upload_id") and entry.get("pending_fingerprint") == current:
        try:
            request = urllib.request.Request(f"{server}/api/v1/uploads/{entry['upload_id']}", method="HEAD", headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                upload = {
                    "id": entry["upload_id"], "upload_url": f"/api/v1/uploads/{entry['upload_id']}",
                    "offset": int(response.headers["Upload-Offset"]), "total_size": path.stat().st_size,
                    "chunk_size": chunk_size, "duplicate": False,
                }
        except urllib.error.HTTPError:
            entry.pop("upload_id", None)
    if upload is None:
        checksum = sha256(path)
        upload = request_json(
            f"{server}/api/v1/uploads", "POST",
            {"filename": path.name, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "total_size": path.stat().st_size, "checksum": checksum, "file_modified_at": __import__("datetime").datetime.fromtimestamp(path.stat().st_mtime, __import__("datetime").timezone.utc).isoformat()},
            headers,
        )
        current["checksum"] = checksum
        if upload.get("duplicate"):
            upload["_fingerprint"] = current
            return upload
        entry["upload_id"] = upload["id"]
        entry["pending_fingerprint"] = current
        save_state(state_path, state)
    offset = int(upload["offset"])
    with path.open("rb") as source:
        source.seek(offset)
        while offset < path.stat().st_size:
            chunk = source.read(min(int(upload.get("chunk_size", chunk_size)), path.stat().st_size - offset))
            request = urllib.request.Request(
                f"{server}{upload['upload_url']}", data=chunk, method="PATCH",
                headers={**headers, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                result = json.loads(response.read())
            offset = int(result["offset"])
            entry["upload_id"] = upload["id"]
            entry["pending_fingerprint"] = current
            save_state(state_path, state)
            if offset == path.stat().st_size:
                upload = result
    upload["_fingerprint"] = current
    return upload


def register_root(server: str, token: str, folder: Path, state: dict[str, Any], requested_root_id: str | None) -> str:
    if requested_root_id:
        state["root_id"] = requested_root_id
        return requested_root_id
    if state.get("root_id"):
        return str(state["root_id"])
    response = request_json(
        f"{server}/api/v1/sync/roots", "POST", {"name": folder.name, "client_id": state["client_id"]}, auth(token)
    )
    state["root_id"] = response["id"]
    return str(response["id"])


def make_operation(kind: str, logical_id: str, base_revision: str | None, relative_path: str | None = None, asset_id: str | None = None, sequence: int = 1) -> dict[str, Any]:
    return {
        "operation_id": str(uuid.uuid4()), "client_sequence": sequence, "kind": kind,
        "logical_id": logical_id, "base_revision": base_revision, "relative_path": relative_path, "asset_id": asset_id,
    }


def local_changes(folder: Path, state_path: Path, state: dict[str, Any], server: str, token: str, chunk_size: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    entries: dict[str, dict[str, Any]] = state["entries"]
    paths = scan(folder, state_path)
    by_path = {entry.get("relative_path"): (logical_id, entry) for logical_id, entry in entries.items()}
    unmatched = dict(entries)
    operations: list[dict[str, Any]] = []
    proposed: dict[str, dict[str, Any]] = {}
    sequence = max((int(item.get("client_sequence", 0)) for item in state.get("recent_operations", [])), default=0) + 1

    for relative_path, path in paths.items():
        direct = by_path.get(relative_path)
        basic = fingerprint(path)
        if direct and {key: direct[1].get("fingerprint", {}).get(key) for key in ("size", "mtime_ns")} == basic:
            unmatched.pop(direct[0], None)
            continue
        checksum = sha256(path)
        basic["checksum"] = checksum
        moved = next(((logical_id, entry) for logical_id, entry in unmatched.items() if entry.get("fingerprint", {}).get("checksum") == checksum), None)
        if moved:
            logical_id, entry = moved
            unmatched.pop(logical_id, None)
            operation = make_operation("move", logical_id, entry.get("revision"), relative_path=relative_path, sequence=sequence)
            proposed[operation["operation_id"]] = {"action": "move", "logical_id": logical_id, "entry": {**entry, "relative_path": relative_path, "fingerprint": basic}}
        else:
            local_entry = direct[1] if direct else {}
            upload = resume_or_create(path, server, token, local_entry, state, state_path, chunk_size)
            asset = upload.get("asset")
            if not asset:
                raise RuntimeError(f"Upload did not return an asset for {relative_path}")
            logical_id = direct[0] if direct else str(uuid.uuid4())
            unmatched.pop(logical_id, None)
            operation = make_operation("upsert", logical_id, local_entry.get("revision"), relative_path, str(asset["id"]), sequence)
            proposed[operation["operation_id"]] = {"action": "upsert", "logical_id": logical_id, "entry": {"relative_path": relative_path, "fingerprint": upload.get("_fingerprint", basic), "revision": local_entry.get("revision")}}
        operations.append(operation)
        sequence += 1

    for logical_id, entry in unmatched.items():
        operation = make_operation("delete", logical_id, entry.get("revision"), entry.get("relative_path"), sequence=sequence)
        operations.append(operation)
        proposed[operation["operation_id"]] = {"action": "delete", "logical_id": logical_id}
        sequence += 1
    return operations, proposed


def apply_push_result(state: dict[str, Any], operations: list[dict[str, Any]], proposed: dict[str, dict[str, Any]], result: dict[str, Any]) -> int:
    entries: dict[str, dict[str, Any]] = state["entries"]
    conflicts = {str(value) for value in result.get("conflicts", [])}
    local_by_id = {operation["operation_id"]: operation for operation in operations}
    applied = 0
    for server_operation in result.get("operations", []):
        operation_id = server_operation["operation_id"]
        proposal = proposed.get(operation_id)
        if proposal is None:
            continue
        if server_operation.get("status") != "applied":
            state["conflicts"].append({"operation_id": operation_id, "logical_id": proposal["logical_id"], "status": server_operation.get("status"), "message": "Server kept both sides until you choose a resolution."})
            continue
        if proposal["action"] == "delete":
            entries.pop(proposal["logical_id"], None)
        else:
            entry = proposal["entry"]
            entry["revision"] = server_operation.get("revision")
            entry.pop("upload_id", None)
            entry.pop("pending_fingerprint", None)
            entries[proposal["logical_id"]] = entry
        applied += 1
    state["recent_operations"] = [{"client_sequence": operation["client_sequence"]} for operation in operations[-20:]]
    return applied


def unchanged_from_state(folder: Path, entry: dict[str, Any]) -> bool:
    path = folder / entry.get("relative_path", "")
    if not path.is_file():
        return False
    saved = entry.get("fingerprint", {})
    basic = fingerprint(path)
    return basic.get("size") == saved.get("size") and basic.get("mtime_ns") == saved.get("mtime_ns")


def download(server: str, token: str, url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".drivebound-download-{uuid.uuid4().hex}.tmp")
    request = urllib.request.Request(f"{server}{url}" if url.startswith("/") else url, headers=auth(token))
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("xb") as output:
            shutil.copyfileobj(response, output, length=4 * 1024 * 1024)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def pull_remote(folder: Path, state: dict[str, Any], server: str, token: str) -> int:
    root_id = state.get("root_id")
    if not root_id:
        return 0
    result = request_json(f"{server}/api/v1/sync/roots/{root_id}/operations?cursor={state.get('cursor', 0)}", headers=auth(token))
    entries: dict[str, dict[str, Any]] = state["entries"]
    applied = 0
    for operation in result.get("operations", []):
        if operation.get("status") != "applied" or operation.get("client_id") == state.get("client_id"):
            continue
        logical_id = operation["logical_id"]
        payload = operation.get("payload") or {}
        current = entries.get(logical_id)
        if operation["kind"] == "delete":
            if current and unchanged_from_state(folder, current):
                (folder / current["relative_path"]).unlink(missing_ok=True)
                entries.pop(logical_id, None)
                applied += 1
            elif current:
                state["conflicts"].append({"logical_id": logical_id, "operation_id": operation["operation_id"], "message": "Remote delete was not applied because the local file changed."})
            continue
        relative_path = payload.get("relative_path")
        original_url = payload.get("original_url")
        if not isinstance(relative_path, str) or not isinstance(original_url, str):
            continue
        destination = (folder / relative_path).resolve()
        if not destination.is_relative_to(folder.resolve()):
            continue
        if current and not unchanged_from_state(folder, current):
            state["conflicts"].append({"logical_id": logical_id, "operation_id": operation["operation_id"], "message": "Remote change was not applied because the local file changed."})
            continue
        download(server, token, original_url, destination)
        entries[logical_id] = {"relative_path": relative_path, "revision": operation.get("revision"), "fingerprint": fingerprint(destination, sha256(destination))}
        applied += 1
    state["cursor"] = max(int(state.get("cursor", 0)), int(result.get("cursor", 0)))
    for conflict_id in result.get("conflicts", []):
        if not any(item.get("server_conflict_id") == str(conflict_id) for item in state["conflicts"]):
            state["conflicts"].append({"server_conflict_id": str(conflict_id), "message": "Resolve this conflict in Drivebound before the two versions can converge."})
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize one selected desktop folder with Drivebound")
    parser.add_argument("folder", type=Path, help="The folder to include in this sync root")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--token", required=True, help="Access token from POST /api/v1/auth/token")
    parser.add_argument("--root-id", help="Join an existing Drivebound sync root from another computer")
    parser.add_argument("--state-file", type=Path, help=f"Defaults to {STATE_NAME} inside the selected folder")
    parser.add_argument("--chunk-size", type=int, default=8 * 1024 * 1024)
    arguments = parser.parse_args()
    folder = arguments.folder.resolve()
    if not folder.is_dir():
        parser.error(f"Folder does not exist: {folder}")
    if arguments.chunk_size <= 0:
        parser.error("--chunk-size must be positive")
    state_path = (arguments.state_file or folder / STATE_NAME).resolve()
    if not state_path.is_relative_to(folder) and arguments.state_file is None:
        parser.error("State file must remain inside the selected folder")
    try:
        state = load_state(state_path)
        server = arguments.server.rstrip("/")
        root_id = register_root(server, arguments.token, folder, state, arguments.root_id)
        pulled = pull_remote(folder, state, server, arguments.token)
        operations, proposed = local_changes(folder, state_path, state, server, arguments.token, arguments.chunk_size)
        pushed = 0
        if operations:
            result = request_json(
                f"{server}/api/v1/sync/roots/{root_id}/operations", "POST",
                {"client_id": state["client_id"], "operations": operations}, auth(arguments.token),
            )
            pushed = apply_push_result(state, operations, proposed, result)
            state["cursor"] = max(int(state.get("cursor", 0)), int(result.get("cursor", 0)))
        save_state(state_path, state)
        print(f"Drivebound sync root {root_id}: {pulled} remote change(s) applied, {pushed} local change(s) acknowledged.")
        if state["conflicts"]:
            print(f"{len(state['conflicts'])} conflict(s) need a decision; no local file was overwritten.", file=sys.stderr)
            return 2
        return 0
    except (OSError, RuntimeError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        print(f"Drivebound sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
