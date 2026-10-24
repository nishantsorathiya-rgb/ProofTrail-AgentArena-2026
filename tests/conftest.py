import ipaddress
import socket

import pytest

from app.application.config import Settings


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    # Ignore any owner credentials/configuration; fixtures must be independently reproducible.
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)
    monkeypatch.setenv("ENABLE_LIVE_REQUESTS", "false")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def assert_local(address):
        if isinstance(address, tuple):
            try:
                local = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                local = False
            if not local:
                raise AssertionError("External networking is forbidden in offline tests.")

    def guarded_connect(sock, address):
        assert_local(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        assert_local(address)
        return original_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in {None, "localhost", "127.0.0.1", "::1"}:
            raise AssertionError("External DNS is forbidden in offline tests.")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'prooftrail.db').as_posix()}",
    )
