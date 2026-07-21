from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest


class ExternalNetworkAccessBlocked(RuntimeError):
    """Raised when a test attempts a non-loopback socket connection."""


def _is_loopback_host(host: Any) -> bool:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False

    normalized_host = host.strip().lower().rstrip(".")
    if normalized_host == "localhost":
        return True

    try:
        ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _is_loopback_socket_address(sock: socket.socket, address: Any) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return True
    if not isinstance(address, tuple) or not address:
        return False

    return _is_loopback_host(address[0])


@pytest.fixture(autouse=True)
def block_non_loopback_socket_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Fail tests before they can open an external network connection."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if not _is_loopback_socket_address(sock, address):
            raise ExternalNetworkAccessBlocked(
                "Tests may connect only to loopback socket addresses"
            )
        original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        if not _is_loopback_socket_address(sock, address):
            raise ExternalNetworkAccessBlocked(
                "Tests may connect only to loopback socket addresses"
            )
        return original_connect_ex(sock, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if (
            not isinstance(address, tuple)
            or not address
            or not _is_loopback_host(address[0])
        ):
            raise ExternalNetworkAccessBlocked(
                "Tests may connect only to loopback socket addresses"
            )
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "") and not _is_loopback_host(host):
            raise ExternalNetworkAccessBlocked(
                "Tests may resolve only loopback network addresses"
            )
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_gethostbyname(host: Any) -> str:
        if not _is_loopback_host(host):
            raise ExternalNetworkAccessBlocked(
                "Tests may resolve only loopback network addresses"
            )
        return original_gethostbyname(host)

    def guarded_gethostbyname_ex(host: Any) -> Any:
        if not _is_loopback_host(host):
            raise ExternalNetworkAccessBlocked(
                "Tests may resolve only loopback network addresses"
            )
        return original_gethostbyname_ex(host)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", guarded_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
    yield
