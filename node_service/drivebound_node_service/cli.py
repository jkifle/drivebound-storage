from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, NodeConfig, pair_with_server
from .service import StorageNodeService


def _default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def _pair_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if args.allow_public_access and args.endpoint_url and not args.endpoint_url.startswith("https://"):
        raise ValueError("Public access requires an HTTPS endpoint URL. Use a reverse proxy or TLS terminator.")
    pair_with_server(args.server, args.pair_code, args.name, config_path, args.endpoint_url, args.allow_public_access)
    print(f"Paired successfully. Config written to {config_path}")
    return 0


def _run_command(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.config)
    if args.endpoint_url:
        config.endpoint_url = args.endpoint_url
    if args.http_port:
        config.http_port = args.http_port
    if args.http_host:
        config.bind_host = args.http_host
    if args.allow_public_access is not None:
        config.allow_public_access = args.allow_public_access
    service = StorageNodeService(config)
    service.run_forever(args.interval)
    return 0


def _status_command(args: argparse.Namespace) -> int:
    config = NodeConfig.load(args.config)
    service = StorageNodeService(config)
    print(f"server: {config.server}")
    print(f"name: {config.name}")
    print(f"node_id: {config.node_id}")
    print(f"storage roots: {service.discover_storage_roots()}")
    print(f"capabilities: {service.capabilities()}")
    return 0


def _install_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if args.platform == "auto":
        if sys.platform.startswith("win"):
            platform = "windows"
        elif sys.platform.startswith("linux"):
            platform = "linux"
        else:
            raise ValueError(f"Unsupported platform detection for {sys.platform!r}")
    else:
        platform = args.platform

    install_dir = Path(args.install_dir) if args.install_dir else None

    if platform == "windows":
        if install_dir is None:
            install_dir = Path.home() / "DriveboundNode" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        script_path = install_dir / "drivebound-node-service.cmd"
        script_path.write_text(
            f"@echo off\r\n"
            f"\"{sys.executable}\" -m drivebound_node_service.cli run --config \"{config_path}\"\r\n",
            encoding="utf-8",
        )
        print(f"Created Windows helper script at {script_path}")
        print("Use Task Scheduler or NSSM to run it in the background as a service.")
        return 0

    if platform == "linux":
        if install_dir is None:
            if os.geteuid() == 0:
                install_dir = Path("/etc/systemd/system")
            else:
                install_dir = config_path.parent
        install_dir = Path(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)
        service_path = install_dir / "drivebound-node.service"
        service_path.write_text(
            "[Unit]\n"
            "Description=Drivebound node service\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={sys.executable} -m drivebound_node_service.cli run --config {config_path}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n",
            encoding="utf-8",
        )
        print(f"Created systemd unit file at {service_path}")
        if os.geteuid() != 0 and str(install_dir) == str(config_path.parent):
            print("To install it system-wide, run: sudo cp \"{path}\" /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now drivebound-node".format(path=service_path))
        else:
            print("Install with: sudo systemctl daemon-reload && sudo systemctl enable --now drivebound-node")
        return 0

    raise ValueError(f"Unsupported platform: {platform}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drivebound storage-node service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pair_parser = subparsers.add_parser("pair", help="Pair this machine to the Drivebound control plane")
    pair_parser.add_argument("server", help="Drivebound API URL, e.g. http://localhost:8000")
    pair_parser.add_argument("--pair-code", required=True, help="Single-use pairing code from Drivebound")
    pair_parser.add_argument("--name", default=os.environ.get("COMPUTERNAME", "Drivebound node"))
    pair_parser.add_argument("--config", default=str(_default_config_path()))
    pair_parser.add_argument("--endpoint-url", default=None, help="Public URL for this node's file API, e.g. https://node.example.com")
    pair_parser.add_argument("--allow-public-access", action="store_true", help="Allow the node HTTP service to listen on all interfaces; default is private-only binding")
    pair_parser.set_defaults(func=_pair_command)

    run_parser = subparsers.add_parser("run", help="Run the node heartbeat loop")
    run_parser.add_argument("--config", default=str(_default_config_path()))
    run_parser.add_argument("--endpoint-url", default=None, help="Override the node endpoint URL advertised to the server")
    run_parser.add_argument("--http-host", default=None, help="Bind address for the local file-serving HTTP API; default is loopback unless --allow-public-access is set")
    run_parser.add_argument("--http-port", type=int, default=None, help="Port for the local file-serving HTTP API")
    run_parser.add_argument("--interval", type=int, default=None, help="Heartbeat interval in seconds")
    run_parser.add_argument("--allow-public-access", action="store_true", help="Bind the node HTTP API to all interfaces")
    run_parser.set_defaults(func=_run_command)

    status_parser = subparsers.add_parser("status", help="Display node configuration and discovered storage")
    status_parser.add_argument("--config", default=str(_default_config_path()))
    status_parser.set_defaults(func=_status_command)

    install_parser = subparsers.add_parser("install", help="Generate OS-specific service config")
    install_parser.add_argument("--platform", choices=["auto", "windows", "linux"], default="auto")
    install_parser.add_argument("--config", default=str(_default_config_path()))
    install_parser.add_argument("--install-dir", default=None, help="Directory to write the generated service file or helper script to")
    install_parser.set_defaults(func=_install_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
