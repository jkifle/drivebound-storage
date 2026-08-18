from fastapi import Request

from app.core.config import settings
from app.main import runtime_configuration_errors
from app.services.accounts import request_context


def make_request(peer: str, forwarded: str | None = None) -> Request:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)})


def test_untrusted_peer_cannot_spoof_forwarded_client_address() -> None:
    original = settings.trusted_proxy_cidrs
    settings.trusted_proxy_cidrs = "10.0.0.0/8"
    try:
        client_ip, _ = request_context(make_request("198.51.100.9", "203.0.113.4"))
        assert client_ip == "198.51.100.9"
    finally:
        settings.trusted_proxy_cidrs = original


def test_trusted_proxy_can_supply_a_valid_client_address() -> None:
    original = settings.trusted_proxy_cidrs
    settings.trusted_proxy_cidrs = "10.10.0.0/16"
    try:
        client_ip, _ = request_context(make_request("10.10.2.4", "203.0.113.7, 10.10.2.4"))
        assert client_ip == "203.0.113.7"
    finally:
        settings.trusted_proxy_cidrs = original


def test_invalid_forwarded_address_falls_back_to_the_direct_peer() -> None:
    original = settings.trusted_proxy_cidrs
    settings.trusted_proxy_cidrs = "127.0.0.1/32"
    try:
        client_ip, _ = request_context(make_request("127.0.0.1", "not-an-ip"))
        assert client_ip == "127.0.0.1"
    finally:
        settings.trusted_proxy_cidrs = original


def test_remote_configuration_rejects_a_global_trusted_proxy_network() -> None:
    original_remote = settings.remote_access_enabled
    original_cidrs = settings.trusted_proxy_cidrs
    settings.remote_access_enabled = True
    settings.trusted_proxy_cidrs = "0.0.0.0/0"
    try:
        assert "TRUSTED_PROXY_CIDRS cannot trust the entire internet" in runtime_configuration_errors()
    finally:
        settings.remote_access_enabled = original_remote
        settings.trusted_proxy_cidrs = original_cidrs
