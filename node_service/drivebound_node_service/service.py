from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import NodeConfig, request_json


class StorageNodeService:
    def __init__(self, config: NodeConfig):
        self.config = config

    @property
    def allowed_roots(self) -> list[Path]:
        roots = [Path(root).expanduser().resolve() for root in self.config.storage_roots] if self.config.storage_roots else []
        if not roots:
            roots = [Path(root).resolve() for root in self.discover_storage_roots()]
        return roots

    @property
    def advertised_endpoint_url(self) -> str:
        if self.config.endpoint_url:
            return self.config.endpoint_url.rstrip("/")
        host = self._detect_local_host()
        return f"http://{host}:{self.config.http_port}"

    def _detect_local_host(self) -> str:
        try:
            hostname = socket.gethostname()
            return socket.gethostbyname(hostname)
        except OSError:
            return "127.0.0.1"

    def discover_storage_roots(self) -> list[str]:
        roots = [str(Path(root).expanduser()) for root in self.config.storage_roots]
        if roots:
            return roots

        if os.name == "nt":
            roots = self._windows_drives()
        else:
            roots = [
                "/",
                "/Volumes",
                "/media",
                "/mnt",
                "/var/lib",
            ]

        unique: list[str] = []
        for root in roots:
            if root not in unique:
                unique.append(root)
        return unique

    def _windows_drives(self) -> list[str]:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        drives: list[str] = []
        for index in range(26):
            if bitmask & (1 << index):
                drives.append(f"{chr(ord('A') + index)}:")
        return drives

    def _resolve_allowed_path(self, requested_path: str | None = None) -> Path:
        roots = self.allowed_roots
        if not roots:
            raise ValueError("No allowed storage roots are configured.")

        if not requested_path:
            return roots[0]

        candidate = Path(requested_path)
        if not candidate.is_absolute():
            candidate = roots[0] / candidate
        resolved = candidate.resolve(strict=False)
        for root in roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise ValueError("Requested path is outside the configured storage roots.")

    def _inventory_for_root(self, root: str, limit: int = 10) -> dict[str, Any]:
        path = Path(root)
        if not path.exists():
            return {"root": root, "available": False, "error": "path_missing", "items": []}
        try:
            entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            return {"root": root, "available": False, "error": str(exc), "items": []}

        items: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            is_dir = entry.is_dir()
            item = {
                "name": entry.name,
                "type": "directory" if is_dir else "file",
                "path": str(entry),
            }
            if is_dir:
                try:
                    item["children_count"] = len(list(entry.iterdir()))
                except OSError:
                    item["children_count"] = 0
            items.append(item)
        return {
            "root": root,
            "available": True,
            "items": items,
            "total_items": len(entries),
            "truncated": len(entries) > limit,
        }

    def browse_directory(self, requested_path: str | None = None, limit: int = 100) -> dict[str, Any]:
        target = self._resolve_allowed_path(requested_path)
        if not target.exists():
            return {"path": str(target), "exists": False, "items": []}
        if not target.is_dir():
            return {"path": str(target), "exists": True, "is_dir": False, "size": target.stat().st_size}

        entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        items: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            is_dir = entry.is_dir()
            item = {
                "name": entry.name,
                "path": str(entry),
                "type": "directory" if is_dir else "file",
                "size": None if is_dir else entry.stat().st_size,
            }
            items.append(item)
        return {
            "path": str(target),
            "exists": True,
            "is_dir": True,
            "items": items,
            "total_items": len(entries),
            "truncated": len(entries) > limit,
            "root": str(self.allowed_roots[0]),
        }

    def read_file(self, path: str) -> bytes:
        file_path = self._resolve_allowed_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(path)
        return file_path.read_bytes()

    def write_file(self, path: str, data: bytes, overwrite: bool = False) -> dict[str, Any]:
        file_path = self._resolve_allowed_path(path)
        if file_path.exists() and not overwrite:
            raise FileExistsError(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        return {"path": str(file_path), "bytes_written": len(data), "overwrite": overwrite}

    def _mount_stats(self, root: str) -> dict[str, Any]:
        path = Path(root)
        if not path.exists():
            return {"root": root, "available": False, "reason": "path_missing"}
        try:
            total, used, free = shutil.disk_usage(path)
        except OSError as exc:
            return {"root": root, "available": False, "reason": str(exc)}
        return {
            "root": root,
            "available": True,
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "inventory": self._inventory_for_root(root),
        }

    def capabilities(self) -> dict[str, Any]:
        mounts = []
        for root in self.discover_storage_roots():
            stats = self._mount_stats(root)
            if stats.get("available"):
                mounts.append(stats)
        return {
            "storage": True,
            "backup": True,
            "mounts": mounts,
            "service_version": self.config.version,
            "endpoint_url": self.advertised_endpoint_url,
        }

    def heartbeat(self) -> dict[str, Any]:
        payload = {
            "version": self.config.version,
            "capabilities": self.capabilities(),
        }
        headers = {"X-Node-Token": self.config.node_secret or ""}
        return request_json(f"{self.config.server.rstrip('/')}/api/v1/nodes/heartbeat", "POST", payload, headers=headers)

    def serve_http(self, host: str | None = None, port: int | None = None) -> None:
        bind_host = host or self.config.bind_host or ("0.0.0.0" if self.config.allow_public_access else "127.0.0.1")
        bind_port = port or self.config.http_port or 8765
        httpd = ThreadingHTTPServer((bind_host, bind_port), self._http_handler_factory())
        httpd.node_service = self
        print(f"Drivebound node HTTP server listening on http://{bind_host}:{bind_port}")
        httpd.serve_forever()

    def _http_handler_factory(self):
        service = self

        class NodeHTTPRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if not self._is_authorized():
                    self._send_json({"detail": "Unauthorized"}, status=401)
                    return

                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    payload = {"status": "ok", "name": service.config.name, "endpoint_url": service.advertised_endpoint_url}
                    self._send_json(payload)
                    return
                if parsed.path in {"/api/v1/files/roots", "/api/v1/files/browse", "/api/v1/files/download"}:
                    if parsed.path == "/api/v1/files/roots":
                        self._send_json({"roots": [str(root) for root in service.allowed_roots]})
                        return
                    if parsed.path == "/api/v1/files/browse":
                        params = parse_qs(parsed.query)
                        requested = params.get("path", [None])[0]
                        try:
                            payload = service.browse_directory(requested)
                        except ValueError as exc:
                            self._send_json({"detail": str(exc)}, status=403)
                            return
                        self._send_json(payload)
                        return
                    if parsed.path == "/api/v1/files/download":
                        params = parse_qs(parsed.query)
                        file_path = params.get("path", [None])[0]
                        if not file_path:
                            self._send_json({"detail": "A file path is required."}, status=400)
                            return
                        try:
                            file_data = service.read_file(file_path)
                        except (FileNotFoundError, ValueError):
                            self._send_json({"detail": "File not found or access denied."}, status=404)
                            return
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(file_data)))
                        self.send_header("Content-Disposition", f"attachment; filename={Path(file_path).name}")
                        self.end_headers()
                        self.wfile.write(file_data)
                        return
                self._send_json({"detail": "Not found."}, status=404)

            def do_POST(self) -> None:
                if not self._is_authorized():
                    self._send_json({"detail": "Unauthorized"}, status=401)
                    return

                parsed = urlparse(self.path)
                if parsed.path != "/api/v1/files/upload":
                    self._send_json({"detail": "Not found."}, status=404)
                    return

                params = parse_qs(parsed.query)
                requested = params.get("path", [None])[0]
                overwrite = params.get("overwrite", ["false"])[0].lower() == "true"
                if not requested:
                    self._send_json({"detail": "A file path is required."}, status=400)
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length else b""
                if not body:
                    self._send_json({"detail": "No file content received."}, status=400)
                    return
                try:
                    result = service.write_file(requested, body, overwrite=overwrite)
                except (ValueError, FileExistsError) as exc:
                    self._send_json({"detail": str(exc)}, status=403 if isinstance(exc, ValueError) else 409)
                    return
                self._send_json(result)

            def _is_authorized(self) -> bool:
                expected = service.config.node_secret or ""
                provided = self.headers.get("X-Drivebound-Node-Auth") or self.headers.get("X-Node-Token")
                return bool(expected) and provided == expected

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003 - standard override
                return

        return NodeHTTPRequestHandler

    def run_forever(self, interval_seconds: int | None = None) -> None:
        interval = interval_seconds if interval_seconds is not None else self.config.heartbeat_interval_seconds
        server_thread = threading.Thread(target=self.serve_http, daemon=True)
        server_thread.start()
        while True:
            try:
                response = self.heartbeat()
                print(f"Heartbeat sent at {time.strftime('%Y-%m-%d %H:%M:%S')} :: {json.dumps(response)}")
            except Exception as exc:  # pragma: no cover - runtime network errors are expected
                print(f"Heartbeat failed at {time.strftime('%Y-%m-%d %H:%M:%S')}: {exc}")
            time.sleep(interval)
