from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib import request

DEFAULT_CONFIG_PATH = Path.home() / ".drivebound-node" / "config.json"


@dataclass
class NodeConfig:
    server: str
    name: str
    node_id: str | None = None
    node_secret: str | None = None
    public_key: str = ""
    endpoint_url: str | None = None
    bind_host: str = "127.0.0.1"
    http_port: int = 8765
    allow_public_access: bool = False
    heartbeat_interval_seconds: int = 60
    storage_roots: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    capabilities: dict[str, Any] = field(default_factory=lambda: {"storage": True, "backup": True})

    @classmethod
    def load(cls, path: str | Path) -> "NodeConfig":
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Node config not found: {config_path}")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return cls(**payload)

    def save(self, path: str | Path) -> Path:
        config_path = Path(path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
        return config_path


def request_json(url: str, method: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with request.urlopen(req, timeout=20) as response:
        body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}


def pair_with_server(
    server: str,
    pair_code: str,
    name: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    endpoint_url: str | None = None,
    allow_public_access: bool = False,
) -> NodeConfig:
    server_url = server.rstrip("/")
    payload = {
        "code": pair_code,
        "name": name,
        "public_key": secrets.token_urlsafe(48),
        "endpoint_url": endpoint_url,
    }
    if allow_public_access and endpoint_url and not endpoint_url.startswith("https://"):
        raise ValueError("Public access requires an HTTPS endpoint URL. Use a reverse proxy or TLS terminator.")
    result = request_json(f"{server_url}/api/v1/nodes/claim", "POST", payload)
    config = NodeConfig(
        server=server_url,
        name=name,
        node_id=result.get("node_id"),
        node_secret=result.get("node_secret"),
        public_key=payload["public_key"],
        endpoint_url=endpoint_url,
        allow_public_access=allow_public_access,
    )
    config.save(config_path)
    return config
