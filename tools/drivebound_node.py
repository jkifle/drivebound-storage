"""Minimal Drivebound storage-node pairing and heartbeat client (standard library only)."""
import argparse
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path


def request(url: str, method: str, data: dict, headers: dict[str, str] | None = None) -> dict:
    payload = json.dumps(data).encode()
    call = urllib.request.Request(url, data=payload, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(call, timeout=20) as response:
        body = response.read()
        return json.loads(body) if body else {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Pair a hard-drive node with Drivebound")
    parser.add_argument("server", help="Drivebound API URL, e.g. https://cloud.example.com")
    parser.add_argument("--config", default=".drivebound-node.json")
    parser.add_argument("--pair-code")
    parser.add_argument("--name", default=os.environ.get("COMPUTERNAME", "Drivebound node"))
    args = parser.parse_args()
    config_path = Path(args.config)
    if args.pair_code:
        result = request(args.server.rstrip("/") + "/api/v1/nodes/claim", "POST", {
            "code": args.pair_code, "name": args.name,
            "public_key": secrets.token_urlsafe(48), "endpoint_url": None,
        })
        config_path.write_text(json.dumps({"server": args.server, **result}, indent=2), encoding="utf-8")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
    config = json.loads(config_path.read_text(encoding="utf-8"))
    while True:
        request(config["server"].rstrip("/") + "/api/v1/nodes/heartbeat", "POST", {
            "version": "0.1.0", "capabilities": {"storage": True, "backup": True}
        }, {"X-Node-Token": config["node_secret"]})
        print("Heartbeat sent")
        time.sleep(60)


if __name__ == "__main__":
    main()
