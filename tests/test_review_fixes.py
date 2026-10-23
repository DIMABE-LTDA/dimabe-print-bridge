"""Pruebas de los hallazgos de la revisión final."""
import threading
import time
from datetime import date

import pytest

from bridge.backends import Target, WindowsBackend
from bridge.capture import CaptureStore
from bridge.config import Settings
from bridge.errors import BridgeError
from bridge.journal import Journal
from bridge.monitor import PrinterMonitor
from bridge.service import PrintService
from tests.test_backends import FakeWin32Print
from tests.test_service import FakeBackend


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "r.sqlite")
    j.start()
    yield j
    j.stop()


# 1. Un sustituto UTF-16 suelto no mata el hilo de la bitácora.
def test_journal_survives_lone_surrogate(journal):
    journal.record(kind="print", target_label="\ud800X", result="ok", message="m")
    journal.record(kind="print", target_label="Boletas", result="ok", message="m")
    journal.flush()
    labels = [r["target_label"] for r in journal.query_day(date.today())]
    assert "Boletas" in labels
    assert len(labels) == 2


def test_journal_queue_is_bounded(tmp_path):
    j = Journal(tmp_path / "q.sqlite", max_queue=3)  # sin start(): nada consume la cola
    for _ in range(10):
        j.record(kind="print", result="ok", message="m")
    assert j._queue.qsize() == 3


# 2. Dedupe: sin carrera y sin colisiones entre impresoras o modo prueba.
def _service(tmp_path, real=None, settings=None):
    settings = settings or Settings()
    j = Journal(tmp_path / "s.sqlite")
    j.start()
    return settings, j, real or FakeBackend(), PrintService(lambda: settings, real or FakeBackend(), FakeBackend(), j)


def test_same_job_id_concurrent_prints_once(tmp_path):
    settings = Settings()
    j = Journal(tmp_path / "c.sqlite")
    j.start()
    real = FakeBackend(delay=0.2)
    service = PrintService(lambda: settings, real, FakeBackend(), j)
    payload = {"printer_name": "A", "raw_hex": "01", "job_id": "job-x"}
    threads = [threading.Thread(target=service.print_job, args=(payload, None, "x")) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    j.stop()
    assert len(real.calls) == 1


def test_same_job_id_different_printers_both_print(tmp_path):
    settings = Settings()
    j = Journal(tmp_path / "d.sqlite")
    j.start()
    real = FakeBackend()
    service = PrintService(lambda: settings, real, FakeBackend(), j)
    service.print_job({"printer_name": "A", "raw_hex": "01", "job_id": "j"}, None, "x")
    service.print_job({"printer_name": "B", "raw_hex": "01", "job_id": "j"}, None, "x")
    j.stop()
    assert len(real.calls) == 2


def test_job_id_from_test_mode_does_not_block_real(tmp_path):
    settings = Settings(test_mode=True)
    j = Journal(tmp_path / "e.sqlite")
    j.start()
    real = FakeBackend()
    service = PrintService(lambda: settings, real, FakeBackend(), j)
    payload = {"printer_name": "A", "raw_hex": "01", "job_id": "j"}
    service.print_job(payload, None, "x")
    settings.test_mode = False
    result = service.print_job(payload, None, "x")
    j.stop()
    assert "duplicate" not in result
    assert len(real.calls) == 1


# 3. La espera por una impresora ocupada tiene límite.
def test_busy_printer_lock_times_out(tmp_path):
    settings = Settings()
    j = Journal(tmp_path / "f.sqlite")
    j.start()
    service = PrintService(lambda: settings, FakeBackend(delay=0.6), FakeBackend(), j, lock_timeout=0.1)
    first = threading.Thread(target=service.print_job, args=({"printer_name": "A", "raw_hex": "01"}, None, "x"))
    first.start()
    time.sleep(0.1)
    with pytest.raises(BridgeError) as exc:
        service.print_job({"printer_name": "A", "raw_hex": "02"}, None, "x")
    first.join()
    j.stop()
    assert exc.value.status == 503
    assert exc.value.code == "printer_busy"


# 4. Sin conexión con la impresora de red → code printer_unreachable (el POS reintenta).
def test_unreachable_network_printer_code():
    def refuse(address, timeout):
        raise ConnectionRefusedError("refused")

    backend = WindowsBackend(win32print=FakeWin32Print(), connect=refuse)
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(Target(kind="ip", ip="192.168.1.50", port=9100), b"x", "job")
    assert exc.value.code == "printer_unreachable"


def test_error_response_carries_code(data_dir):
    from bridge.server import build_context, create_local_app

    def refuse(address, timeout):
        raise ConnectionRefusedError("refused")

    ctx = build_context(real=WindowsBackend(win32print=FakeWin32Print(), connect=refuse))
    try:
        r = create_local_app(ctx).test_client().post(
            "/api/print", json={"ip_address": "192.168.1.50", "raw_hex": "1B40"}, headers={"Host": "127.0.0.1:3210"})
        assert r.get_json()["code"] == "printer_unreachable"
        assert r.get_json()["result"] is False
    finally:
        ctx.journal.stop()


# 5. Nombres UNC: solo si Windows ya tiene esa impresora.
def test_unc_printer_name_rejected_unless_installed(tmp_path):
    settings = Settings()
    j = Journal(tmp_path / "g.sqlite")
    j.start()
    real = FakeBackend()
    real.list_printers = lambda: [{"Name": "\\\\server\\Cocina"}]
    service = PrintService(lambda: settings, real, FakeBackend(), j)
    with pytest.raises(BridgeError) as exc:
        service.parse_target({"printer_name": "\\\\10.0.0.5\\x"})
    assert exc.value.status == 400
    assert service.parse_target({"printer_name": "\\\\server\\Cocina"}).printer_name == "\\\\server\\Cocina"
    j.stop()


# 6. Topes de almacenamiento.
def test_large_raw_payload_not_stored(tmp_path):
    settings = Settings()
    j = Journal(tmp_path / "h.sqlite")
    j.start()
    service = PrintService(lambda: settings, FakeBackend(), FakeBackend(), j)
    service.print_job({"printer_name": "A", "raw_hex": "00" * 70_000}, None, "x")
    service.print_job({"printer_name": "A", "raw_hex": "1B40"}, None, "x")
    j.flush()
    rows = {r["bytes"]: j.get(r["id"])["payload"] for r in j.query_day(date.today())}
    j.stop()
    assert rows[70_000] is None
    assert rows[2] == "1b40"


def test_capture_store_total_bytes_bounded():
    store = CaptureStore(max_items=500, max_bytes=1000)
    for _ in range(5):
        store.add("x", "receipt", "raw", b"a" * 400)
    assert sum(len(j.data) for j in store.list()) <= 1000
    assert len(store.list()) == 2


# 9. El monitor no sondea una impresora que está imprimiendo.
def test_monitor_skips_busy_target(journal):
    journal.record(kind="print", target_label="192.168.1.50:9100", target_kind="ip", result="ok", message="m")
    journal.flush()
    calls = []

    def connect(address, timeout):
        calls.append(address)
        raise OSError("no")

    busy = {"192.168.1.50:9100"}
    monitor = PrinterMonitor(FakeBackend(), journal, lambda: Settings(), connect=connect,
                             is_busy=lambda label: label in busy)
    monitor.refresh_now()
    assert calls == []
    busy.clear()
    monitor.refresh_now()
    assert len(calls) == 1
