import socket

import pytest

from bridge.config import Settings
from bridge.server import ServerRunner, build_context, ensure_local_token
from bridge.tray_client import TrayClient
from bridge.watchdog import Watchdog
from tests.test_service import FakeBackend


def test_watchdog_fails_on_third_consecutive_failure():
    results = iter([False, False, True, False, False, False])
    fails = []
    dog = Watchdog(check=lambda: next(results), on_fail=lambda: fails.append(1), failures=3)
    for _ in range(6):
        dog.run_once()
    assert fails == [1]


def test_watchdog_check_exception_counts_as_failure():
    fails = []

    def boom():
        raise OSError("sin respuesta")

    dog = Watchdog(check=boom, on_fail=lambda: fails.append(1), failures=2)
    dog.run_once()
    dog.run_once()
    assert fails == [1]


def test_local_token_persists(data_dir):
    first = ensure_local_token()
    assert len(first) >= 32
    assert ensure_local_token() == first
    assert (data_dir / "local-token").read_text(encoding="utf-8").strip() == first


def test_context_uses_local_token(data_dir):
    token = ensure_local_token()
    ctx = build_context(real=FakeBackend())
    try:
        assert ctx.csrf_token == token
    finally:
        ctx.journal.stop()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running(data_dir):
    port = free_port()
    ctx = build_context(real=FakeBackend(), settings=Settings(port=port))
    runner = ServerRunner(ctx, retry_seconds=0.1)
    runner.start()
    for _ in range(50):
        if runner.state == "running":
            break
        __import__("time").sleep(0.1)
    yield ctx, port
    runner.stop()
    ctx.journal.stop()


def test_tray_client_status_and_test_mode(running):
    ctx, port = running
    client = TrayClient(port, ctx.csrf_token)
    status = client.status()
    assert status["service"]["version"]
    client.set_test_mode(True)
    assert ctx.settings.test_mode is True
    assert client.status()["service"]["test_mode"] is True


def test_tray_client_bad_token_rejected(running):
    _, port = running
    with pytest.raises(Exception):
        TrayClient(port, "otro").set_test_mode(True)


def test_tray_client_unreachable_returns_none():
    assert TrayClient(free_port(), "x", timeout=0.5).status() is None
