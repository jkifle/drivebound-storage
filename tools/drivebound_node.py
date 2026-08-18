#!/usr/bin/env python3
"""Drivebound storage-node pairing, Ed25519 attestation, and heartbeat client."""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError as exc:  # pragma: no cover - exercised only on an unprepared node host
    raise SystemExit("drivebound_node.py requires the 'cryptography' package") from exc

NODE_VERSION = "0.2.0"
DEFAULT_CAPABILITIES: dict[str, object] = {"storage": True, "backup": True}


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a node token/signature through an HTTP redirect."""

    def redirect_request(self, request, file_pointer, code, message, response_headers, new_url):
        raise urllib.error.HTTPError(
            request.full_url,
            code,
            "Drivebound node requests do not follow redirects",
            response_headers,
            file_pointer,
        )


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Drivebound" / "node.json"


def validate_server_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Server must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Server URL must not contain credentials, a query, or a fragment")
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("HTTPS is required for every non-loopback Drivebound server")
    return url.rstrip("/")


def encode_base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_base64url(value: str, expected_bytes: int) -> bytes:
    if not value or "=" in value:
        raise ValueError("Expected canonical unpadded base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError("Expected canonical unpadded base64url") from exc
    if len(raw) != expected_bytes or encode_base64url(raw) != value:
        raise ValueError("Encoded Ed25519 material has an invalid length")
    return raw


def canonical_payload(action: str, **fields: Any) -> bytes:
    return json.dumps(
        {"action": action, **fields},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def heartbeat_payload(
    node_id: str,
    timestamp_ms: int,
    version: str | None,
    capabilities: dict[str, object],
) -> bytes:
    return canonical_payload(
        "heartbeat",
        node_id=str(uuid.UUID(node_id)),
        timestamp_ms=timestamp_ms,
        version=version,
        capabilities=capabilities,
    )


def claim_payload(
    code: str,
    name: str,
    public_key: str,
    timestamp_ms: int,
    endpoint_url: str | None,
) -> bytes:
    return canonical_payload(
        "claim",
        code=code.upper(),
        name=name,
        public_key=public_key,
        timestamp_ms=timestamp_ms,
        endpoint_url=endpoint_url,
    )


def rotation_payload(node_id: str, timestamp_ms: int) -> bytes:
    return canonical_payload("rotate_secret", node_id=str(uuid.UUID(node_id)), timestamp_ms=timestamp_ms)


def generate_identity() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return encode_base64url(private_raw), encode_base64url(public_raw)


def sign(private_key: str, payload: bytes) -> str:
    raw = decode_base64url(private_key, 32)
    return encode_base64url(Ed25519PrivateKey.from_private_bytes(raw).sign(payload))


def public_key_for_private(private_key: str) -> str:
    raw = decode_base64url(private_key, 32)
    public_raw = Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return encode_base64url(public_raw)


def request(url: str, method: str, data: dict, headers: dict[str, str] | None = None) -> dict:
    # Re-check the final request rather than relying only on CLI validation;
    # stored configurations are untrusted input too.
    validate_server_url(url)
    payload = json.dumps(data, allow_nan=False).encode("utf-8")
    call = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )

    opener = urllib.request.build_opener(RejectRedirectHandler())
    with opener.open(call, timeout=20) as response:
        body = response.read()
        return json.loads(body) if body else {}


def _restrict_windows_path(path: Path, *, directory: bool = False) -> None:
    username = getpass.getuser()
    user_permission = f"{username}:(OI)(CI)(F)" if directory else f"{username}:(F)"
    system_permission = "*S-1-5-18:(OI)(CI)(F)" if directory else "*S-1-5-18:(F)"
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            user_permission,
            system_permission,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        target = "directory" if directory else "file"
        raise RuntimeError(f"Could not restrict the node config {target} to the current user and SYSTEM")


def restrict_config_file(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_path(path)
        return
    os.chmod(path, 0o600)
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Node config permissions must not allow group or other access")


def prepare_config_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != default_config_path().parent.resolve():
        return
    if os.name == "nt":
        _restrict_windows_path(path.parent, directory=True)
    else:
        os.chmod(path.parent, 0o700)


def write_config(path: Path, config: dict[str, Any]) -> None:
    prepare_config_directory(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # On Windows, tighten the empty file before any credential bytes are
        # written. The default parent is restricted first as defense in depth.
        if os.name == "nt":
            try:
                restrict_config_file(temporary)
            except Exception:
                os.close(descriptor)
                raise
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        restrict_config_file(temporary)
        os.replace(temporary, path)
        restrict_config_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"Node config does not exist: {path}")
    if os.name == "nt":
        # Re-apply the intended ACL in case a backup/restore operation inherited
        # broader permissions than the atomic writer established.
        prepare_config_directory(path)
        restrict_config_file(path)
    elif path.stat().st_mode & 0o077:
        raise RuntimeError("Refusing to read a node config accessible by group or other users")
    config = json.loads(path.read_text(encoding="utf-8"))
    for required in ("server", "node_id", "node_secret", "private_key", "public_key"):
        if not config.get(required):
            raise RuntimeError("Legacy or incomplete node config must be explicitly re-paired")
    decode_base64url(str(config["private_key"]), 32)
    decode_base64url(str(config["public_key"]), 32)
    if public_key_for_private(str(config["private_key"])) != config["public_key"]:
        raise RuntimeError("Node config public key does not match its private identity")
    config["server"] = validate_server_url(str(config["server"]))
    return config


def reserve_timestamp(config_path: Path, config: dict[str, Any]) -> int:
    timestamp_ms = max(int(time.time() * 1000), int(config.get("last_timestamp_ms", 0)) + 1)
    config["last_timestamp_ms"] = timestamp_ms
    # Persist before sending. If the response is lost, the next signature still
    # cannot replay a timestamp the server may already have accepted.
    write_config(config_path, config)
    return timestamp_ms


def pair_node(args: argparse.Namespace, config_path: Path) -> dict[str, Any]:
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing.get("private_key") and not args.replace_existing:
            raise RuntimeError("Config already has an attested identity; use --replace-existing only for an intentional re-pair")
    private_key, public_key = generate_identity()
    timestamp_ms = int(time.time() * 1000)
    endpoint_url = None
    code = args.pair_code.upper()
    signature = sign(
        private_key,
        claim_payload(code, args.name, public_key, timestamp_ms, endpoint_url),
    )
    result = request(args.server.rstrip("/") + "/api/v1/nodes/claim", "POST", {
        "code": code,
        "name": args.name,
        "public_key": public_key,
        "timestamp_ms": timestamp_ms,
        "signature": signature,
        "endpoint_url": endpoint_url,
    })
    config = {
        "schema": 2,
        "server": args.server,
        "node_id": result["node_id"],
        "node_secret": result["node_secret"],
        "private_key": private_key,
        "public_key": public_key,
        "last_timestamp_ms": timestamp_ms,
    }
    write_config(config_path, config)
    return config


def rotate_secret(config_path: Path, config: dict[str, Any]) -> None:
    timestamp_ms = reserve_timestamp(config_path, config)
    signature = sign(config["private_key"], rotation_payload(config["node_id"], timestamp_ms))
    result = request(
        config["server"].rstrip("/") + "/api/v1/nodes/rotate-secret",
        "POST",
        {"node_id": config["node_id"], "timestamp_ms": timestamp_ms, "signature": signature},
        {"X-Node-Token": config["node_secret"]},
    )
    config["node_secret"] = result["node_secret"]
    write_config(config_path, config)


def send_heartbeat(config_path: Path, config: dict[str, Any]) -> None:
    timestamp_ms = reserve_timestamp(config_path, config)
    signature = sign(
        config["private_key"],
        heartbeat_payload(config["node_id"], timestamp_ms, NODE_VERSION, DEFAULT_CAPABILITIES),
    )
    request(
        config["server"].rstrip("/") + "/api/v1/nodes/heartbeat",
        "POST",
        {
            "node_id": config["node_id"],
            "timestamp_ms": timestamp_ms,
            "version": NODE_VERSION,
            "capabilities": DEFAULT_CAPABILITIES,
            "signature": signature,
        },
        {"X-Node-Token": config["node_secret"]},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Pair an attested hard-drive node with Drivebound")
    parser.add_argument("server", help="Drivebound API URL, e.g. https://cloud.example.com")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--pair-code")
    parser.add_argument("--name", default=os.environ.get("COMPUTERNAME", "Drivebound node"))
    parser.add_argument("--replace-existing", action="store_true", help="Replace an existing attested identity during re-pair")
    parser.add_argument("--rotate-secret", action="store_true", help="Rotate the token using the node's Ed25519 identity")
    parser.add_argument("--once", action="store_true", help="Send one heartbeat and exit")
    args = parser.parse_args()
    args.server = validate_server_url(args.server)
    config_path = Path(args.config).resolve()

    config = pair_node(args, config_path) if args.pair_code else read_config(config_path)
    if args.rotate_secret:
        rotate_secret(config_path, config)
    while True:
        send_heartbeat(config_path, config)
        print("Signed heartbeat sent")
        if args.once:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
