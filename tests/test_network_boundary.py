from __future__ import annotations

import socket

import pytest

from conftest import ExternalNetworkAccessBlocked


@pytest.mark.parametrize(
    "address",
    [
        ("192.0.2.1", 443),
        ("example.invalid", 443),
    ],
)
def test_non_loopback_connect_is_blocked_before_socket_io(
    address: tuple[str, int],
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.raises(
            ExternalNetworkAccessBlocked,
            match="only to loopback",
        ):
            client.connect(address)


def test_non_loopback_connect_ex_is_blocked_before_socket_io() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.raises(
            ExternalNetworkAccessBlocked,
            match="only to loopback",
        ):
            client.connect_ex(("198.51.100.1", 443))


def test_non_loopback_create_connection_is_blocked_before_dns_or_socket_io() -> None:
    with pytest.raises(
        ExternalNetworkAccessBlocked,
        match="only to loopback",
    ):
        socket.create_connection(("example.invalid", 443), timeout=0.1)


@pytest.mark.parametrize(
    "resolver",
    [
        lambda: socket.getaddrinfo("example.invalid", 443),
        lambda: socket.gethostbyname("example.invalid"),
        lambda: socket.gethostbyname_ex("example.invalid"),
    ],
)
def test_non_loopback_dns_resolution_is_blocked_before_lookup(resolver) -> None:
    with pytest.raises(
        ExternalNetworkAccessBlocked,
        match="only loopback",
    ):
        resolver()


def test_loopback_dns_resolution_remains_available() -> None:
    results = socket.getaddrinfo("localhost", 80)

    assert results
    assert all(item[4][0] in {"127.0.0.1", "::1"} for item in results)


def test_loopback_connection_remains_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)

        with socket.create_connection(server.getsockname(), timeout=2) as client:
            accepted, _ = server.accept()
            with accepted:
                client.sendall(b"praxis")
                assert accepted.recv(6) == b"praxis"
