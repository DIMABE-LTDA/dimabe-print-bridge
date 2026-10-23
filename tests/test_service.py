import base64
import threading
import time
from datetime import date

import pytest

from bridge.backends import Target
from bridge.config import Settings
from bridge.errors import BridgeError
from bridge.journal import Journal
from bridge.service import PrintService


class FakeBackend:
    def __init__(self, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.intervals = []
        self._lock = threading.Lock()

    def _do(self, kind, target, data):
        start = time.monotonic()
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise BridgeError(self.fail, 500)
        with self._lock:
            self.calls.append((kind, target, data))
            self.intervals.append((target.label, start, time.monotonic()))

    def send_raw(self, target, data, job_name):
        self._do("raw", target, data)

    def print_image(self, target, image, paper_width_mm, job_name):
        self._do("image", target, image)

    def open_cashbox(self, target):
        self._do("cashbox", target, b"")

    def list_printers(self):
        return []

    def queue_status(self, name):
        return "ok"


@pytest.fixture
def env(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "b.sqlite")
    journal.start()
    real, virtual = FakeBackend(), FakeBackend()
    now = [1000.0]
    service = PrintService(lambda: settings, real, virtual, journal, clock=lambda: now[0])
    yield settings, journal, real, virtual, service, now
    journal.stop()


def rows(journal):
    journal.flush()
    return journal.query_day(date.today())


def test_ip_wins_and_validated(env):
    _, _, _, _, service, _ = env
    target = service.parse_target({"printer_name": "BIXOLON", "ip_address": "192.168.1.50", "ip_port": 9101})
    assert target == Target(kind="ip", ip="192.168.1.50", port=9101)
    with pytest.raises(BridgeError) as exc:
        service.parse_target({"ip_address": "8.8.8.8"})
    assert exc.value.status == 400


def test_missing_target(env):
    with pytest.raises(BridgeError) as exc:
        env[4].parse_target({})
    assert exc.value.message == "A printer target is required (printer_name for USB or ip_address for network)."
    assert exc.value.status == 400


def test_raw_hex_print(env):
    _, journal, real, _, service, _ = env
    result = service.print_job({"printer_name": "BIXOLON", "raw_hex": "1B 40 41"}, "https://x.odoo.com", "127.0.0.1")
    assert result == {"result": True, "status": "success", "message": "Raw bytes sent to 'BIXOLON'.", "test_mode": False}
    assert real.calls == [("raw", Target(kind="usb", printer_name="BIXOLON"), b"\x1b@A")]
    row = rows(journal)[0]
    assert (row["kind"], row["result"], row["bytes"], row["document_type"]) == ("print", "ok", 3, "receipt")


def test_empty_raw_base64_falls_back_to_receipt(env):
    _, _, real, _, service, _ = env
    image = base64.b64encode(b"jpegdata").decode()
    result = service.print_job({"printer_name": "B", "raw_base64": "", "receipt": "data:image/jpeg;base64," + image}, None, "127.0.0.1")
    assert result["message"] == "Printed on 'B'."
    assert real.calls[0][0] == "image" and real.calls[0][2] == b"jpegdata"


def test_bad_hex_is_400(env):
    with pytest.raises(BridgeError) as exc:
        env[4].print_job({"printer_name": "B", "raw_hex": "ABC"}, None, "x")
    assert exc.value.status == 400


def test_no_content_is_400(env):
    with pytest.raises(BridgeError) as exc:
        env[4].print_job({"printer_name": "B"}, None, "x")
    assert exc.value.status == 400


def test_backend_error_recorded_and_raised(env):
    settings, journal, _, _, _, now = env
    failing = FakeBackend(fail="No hubo conexión")
    service = PrintService(lambda: settings, failing, FakeBackend(), journal, clock=lambda: now[0])
    with pytest.raises(BridgeError):
        service.print_job({"ip_address": "192.168.1.50", "raw_hex": "1B40"}, None, "x")
    row = rows(journal)[0]
    assert (row["result"], row["message"], row["target_kind"]) == ("error", "No hubo conexión", "ip")


def test_dedupe_job_id_expires(env):
    _, journal, real, _, service, now = env
    payload = {"printer_name": "B", "raw_hex": "1B40", "job_id": "job-1"}
    service.print_job(payload, None, "x")
    again = service.print_job(payload, None, "x")
    assert again["duplicate"] is True and again["result"] is True
    assert len(real.calls) == 1
    now[0] += 301
    service.print_job(payload, None, "x")
    assert len(real.calls) == 2
    assert [r["result"] for r in rows(journal)].count("duplicate") == 1


def test_failed_job_id_can_retry(env):
    settings, journal, _, _, _, now = env
    failing = FakeBackend(fail="boom")
    service = PrintService(lambda: settings, failing, FakeBackend(), journal, clock=lambda: now[0])
    payload = {"printer_name": "B", "raw_hex": "1B40", "job_id": "job-2"}
    with pytest.raises(BridgeError):
        service.print_job(payload, None, "x")
    failing.fail = None
    assert "duplicate" not in service.print_job(payload, None, "x")


def test_test_mode_never_touches_real(env):
    settings, journal, real, virtual, service, _ = env
    settings.test_mode = True
    result = service.print_job({"printer_name": "B", "raw_hex": "1B40"}, None, "x")
    service.cashbox({"printer_name": "B"}, None, "x")
    assert result["test_mode"] is True
    assert real.calls == []
    assert [c[0] for c in virtual.calls] == ["raw", "cashbox"]
    assert all(r["test_mode"] == 1 for r in rows(journal))


def test_cashbox_message(env):
    result = env[4].cashbox({"ip_address": "192.168.1.50"}, None, "x")
    assert result["message"] == "Cash drawer pulse sent to '192.168.1.50:9100'."


def test_payload_storage_rules(env):
    settings, journal, _, _, service, _ = env
    image = base64.b64encode(b"jpeg").decode()
    service.print_job({"printer_name": "B", "raw_hex": "1B40"}, None, "x")
    service.print_job({"printer_name": "B", "receipt": image}, None, "x")
    by_kind = {r["payload_kind"]: journal.get(r["id"])["payload"] for r in rows(journal)}
    assert by_kind == {"raw": "1b40", "image": None}


def test_reprint_resends_saved_payload(env):
    _, journal, real, _, service, _ = env
    service.print_job({"printer_name": "B", "raw_hex": "1B4041"}, None, "x")
    event_id = rows(journal)[0]["id"]
    result = service.reprint(event_id)
    assert result["result"] is True
    assert real.calls[-1][2] == b"\x1b@A"
    with pytest.raises(BridgeError) as exc:
        service.reprint(99999)
    assert exc.value.status == 404


def test_same_target_serialized_different_targets_parallel(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "c.sqlite")
    journal.start()
    real = FakeBackend(delay=0.2)
    service = PrintService(lambda: settings, real, FakeBackend(), journal)
    jobs = [{"printer_name": "A", "raw_hex": "01"}, {"printer_name": "A", "raw_hex": "02"},
            {"printer_name": "B", "raw_hex": "03"}]
    threads = [threading.Thread(target=service.print_job, args=(p, None, "x")) for p in jobs]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    journal.stop()
    a = sorted((s, e) for label, s, e in real.intervals if label == "A")
    assert a[0][1] <= a[1][0] + 0.01, "dos trabajos a la misma impresora se solaparon"
    assert elapsed < 0.55, "impresoras distintas no corrieron en paralelo"
