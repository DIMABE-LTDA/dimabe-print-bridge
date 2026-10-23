import contextlib
import socket
import time
from datetime import date

import pytest

from bridge.config import Settings
from bridge.journal import Journal
from bridge.monitor import PrinterMonitor
from bridge.server import ServerRunner, build_context
from tests.test_service import FakeBackend


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "m.sqlite")
    j.start()
    yield j
    j.stop()


def record(journal, label, kind, result="ok"):
    journal.record(kind="print", target_label=label, target_kind=kind, result=result, message="m")


def test_monitor_ip_status(journal):
    record(journal, "192.168.1.50:9100", "ip")
    record(journal, "192.168.1.51:9100", "ip")
    journal.flush()

    @contextlib.contextmanager
    def connect(address, timeout):
        if address[0] == "192.168.1.50":
            raise OSError("sin respuesta")
        yield None

    monitor = PrinterMonitor(FakeBackend(), journal, lambda: Settings(), connect=connect)
    monitor.refresh_now()
    snap = {p["label"]: p for p in monitor.snapshot()}
    assert snap["192.168.1.50:9100"]["status"] == "error"
    assert snap["192.168.1.50:9100"]["status_text"] == "No responde"
    assert snap["192.168.1.51:9100"]["status"] == "ok"
    assert snap["192.168.1.51:9100"]["today_ok"] == 1


def test_monitor_usb_queue_states(journal):
    record(journal, "Pausada", "usb")
    record(journal, "Lista", "usb")
    journal.flush()
    backend = FakeBackend()
    backend.queue_status = lambda name: "paused" if name == "Pausada" else "ok"
    monitor = PrinterMonitor(backend, journal, lambda: Settings())
    monitor.refresh_now()
    snap = {p["label"]: p for p in monitor.snapshot()}
    assert (snap["Pausada"]["status"], snap["Pausada"]["status_text"]) == ("warn", "Pausada en Windows")
    assert snap["Lista"]["status"] == "ok"


def test_monitor_test_mode_marks_virtual(journal):
    record(journal, "192.168.1.50:9100", "ip")
    journal.flush()
    monitor = PrinterMonitor(FakeBackend(), journal, lambda: Settings(test_mode=True))
    monitor.refresh_now()
    assert monitor.snapshot()[0]["status"] == "test"


def test_runner_waits_for_busy_port(data_dir):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    ctx = build_context(real=FakeBackend(), settings=Settings(port=port))
    runner = ServerRunner(ctx, retry_seconds=0.1)
    try:
        runner.start()
        time.sleep(0.3)
        assert runner.state == "port_busy"
        blocker.close()
        for _ in range(50):
            if runner.state == "running":
                break
            time.sleep(0.1)
        assert runner.state == "running"
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            conn.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            response = b""
            while chunk := conn.recv(4096):
                response += chunk
            assert b"Dimabe Print Bridge is running." in response
    finally:
        runner.stop()
        ctx.journal.stop()
