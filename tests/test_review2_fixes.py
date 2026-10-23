"""Hallazgos de la revisión final de la resiliencia (puente)."""
import socket
import time
from datetime import datetime

import pytest

from bridge.backends import Target
from bridge.config import Settings, save_settings
from bridge.journal import Journal
from bridge.outbox import Outbox
from bridge.outbox_worker import OutboxWorker
from bridge.server import ServerRunner, build_context
from bridge.service import PrintService
from bridge.watchdog import health_check
from tests.test_resilience_service import RoutedBackend
from tests.test_service import FakeBackend
from tests.test_updater import PIN, Harness

IP = Target(kind="ip", ip="192.168.1.50", port=9100)
BAR = Target(kind="ip", ip="192.168.1.51", port=9100)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# C1: el vigilante debe pasar la clave API (si no, reinicia el servicio para siempre).
def test_watchdog_check_sends_api_key(data_dir):
    port = free_port()
    settings = Settings(port=port, api_key="clave-secreta")
    ctx = build_context(real=FakeBackend(), settings=settings)
    runner = ServerRunner(ctx, retry_seconds=0.1)
    runner.start()
    try:
        for _ in range(50):
            if runner.state == "running":
                break
            time.sleep(0.1)
        assert health_check(lambda: ctx.settings.port, lambda: ctx.settings.api_key)() is True
        with pytest.raises(Exception):
            health_check(lambda: ctx.settings.port, lambda: "")()
    finally:
        runner.stop()
        ctx.journal.stop()


# I1: firma con contenido alterado (HashMismatch) se rechaza aunque la huella coincida.
def test_hash_mismatch_signature_rejected(tmp_path):
    h = Harness(tmp_path)
    h.updater.verify = lambda path: (PIN, "HashMismatch")
    try:
        h.updater.check()
        assert h.updater.maybe_install() == "rejected:signature"
        assert h.installed == []
    finally:
        h.close()


# C2: si el archivo cambia entre la verificación y la ejecución, no se ejecuta.
def test_installer_swapped_after_verify_is_not_run(tmp_path):
    h = Harness(tmp_path)

    def verify_then_swap(path):
        path.write_bytes(b"MZ cambiado por otro")
        return PIN, "UnknownError"

    h.updater.verify = verify_then_swap
    try:
        h.updater.check()
        assert h.updater.maybe_install() == "rejected:changed"
        assert h.installed == []
    finally:
        h.close()


# I2: una versión que falló no se reintenta la noche siguiente; no se lanzan dos instaladores.
def test_failed_version_is_skipped(tmp_path):
    h = Harness(tmp_path)
    try:
        h.settings.update_failed_version = "1.1.0.7"
        h.updater.check()
        assert h.updater.maybe_install() == "skipped:failed_before"
    finally:
        h.close()


def test_no_second_install_while_one_is_running(tmp_path):
    h = Harness(tmp_path)
    try:
        h.updater.check()
        assert h.updater.maybe_install() == "installed"
        assert h.updater.maybe_install() == "skipped:installing"
        assert len(h.installed) == 1
    finally:
        h.close()


def test_pending_marker_with_old_version_marks_failure(tmp_path):
    h = Harness(tmp_path, current="1.1.0.5")
    try:
        h.updater.check()
        h.updater.maybe_install()
        # Reinicio tras el intento: sigue corriendo la versión vieja → la nueva falló.
        again = Harness(tmp_path, current="1.1.0.5")
        again.settings = h.settings
        again.updater.settings_provider = lambda: again.settings
        again.updater.record_startup()
        assert again.settings.update_failed_version == "1.1.0.7"
        again.close()
    finally:
        h.close()


# I3: dos procesos sobre la misma cola no imprimen el mismo trabajo dos veces.
def test_queue_lease_prevents_double_delivery(tmp_path):
    first = Outbox(tmp_path / "cola.sqlite")
    second = Outbox(tmp_path / "cola.sqlite")
    first.add("j1", IP, "kitchen", b"x")
    assert [j.job_id for j in first.claim_due()] == ["j1"]
    assert second.claim_due() == []


def test_expired_lease_is_recovered(tmp_path):
    now = [1000.0]
    box = Outbox(tmp_path / "cola.sqlite", clock=lambda: now[0])
    box.add("j1", IP, "kitchen", b"x")
    assert len(box.claim_due()) == 1
    now[0] += 301
    assert [j.job_id for j in box.claim_due()] == ["j1"]


# I4: una impresora caída no frena la cola de las demás.
def test_down_printer_does_not_block_other_targets(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "j.sqlite")
    journal.start()
    outbox = Outbox(tmp_path / "cola.sqlite")
    real = RoutedBackend(down={IP.label})
    attempts = []
    original = real.send_raw

    def counting(target, data, job_name):
        attempts.append(target.label)
        return original(target, data, job_name)

    real.send_raw = counting
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    for i in range(5):
        outbox.add(f"c{i}", IP, "kitchen", b"x")
    outbox.add("b1", BAR, "kitchen", b"x")
    OutboxWorker(outbox, service, lambda: settings).run_once()
    journal.stop()
    assert attempts.count(IP.label) == 1
    assert real.calls == [(BAR.label, b"x")]


def test_worker_idle_when_not_serving(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "j.sqlite")
    journal.start()
    outbox = Outbox(tmp_path / "cola.sqlite")
    real = RoutedBackend()
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    outbox.add("c1", IP, "kitchen", b"x")
    OutboxWorker(outbox, service, lambda: settings, is_active=lambda: False).run_once()
    journal.stop()
    assert real.calls == []


# Menor: si el respaldo imprimió, el trabajo de la principal queda como hecho.
def test_backup_success_marks_primary_job_done(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "j.sqlite")
    journal.start()
    outbox = Outbox(tmp_path / "cola.sqlite")
    real = RoutedBackend(down={IP.label})
    service = PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)
    payload = {"ip_address": "192.168.1.50", "raw_hex": "1B40", "document_type": "kitchen", "job_id": "k1",
               "fallback_target": {"ip_address": "192.168.1.60"}}
    service.print_job(payload, None, "x")
    real.down.clear()
    again = service.print_job(payload, None, "x")
    journal.stop()
    assert again.get("duplicate") is True
    assert len(real.calls) == 1
