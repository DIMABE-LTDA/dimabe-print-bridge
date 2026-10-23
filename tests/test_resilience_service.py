import socket

import pytest

from bridge.backends import Target, WindowsBackend
from bridge.config import Settings
from bridge.errors import BridgeError
from bridge.journal import Journal
from bridge.outbox import Outbox
from bridge.service import PrintService
from tests.test_backends import FakeWin32Print

IP = {"ip_address": "192.168.1.50", "ip_port": 9100}
BACKUP = {"ip_address": "192.168.1.60", "ip_port": 9100}


class RoutedBackend:
    """Backend falso: los destinos de `down` fallan como impresora inalcanzable."""

    def __init__(self, down=(), code="printer_unreachable"):
        self.down = set(down)
        self.code = code
        self.calls = []

    def send_raw(self, target, data, job_name):
        if target.label in self.down:
            raise BridgeError(f"No hubo conexión con {target.label}", 500, code=self.code)
        self.calls.append((target.label, data))

    def print_image(self, target, image, paper_width_mm, job_name):
        if target.label in self.down:
            raise BridgeError("caída", 500, code=self.code)
        self.calls.append((target.label, image))

    def open_cashbox(self, target):
        self.calls.append((target.label, b""))

    def list_printers(self):
        return []

    def queue_status(self, name):
        return "ok"


@pytest.fixture
def env(tmp_path):
    settings = Settings()
    journal = Journal(tmp_path / "j.sqlite")
    journal.start()
    outbox = Outbox(tmp_path / "cola.sqlite")

    def make(real):
        return PrintService(lambda: settings, real, RoutedBackend(), journal, outbox=outbox)

    yield settings, outbox, make
    journal.stop()


def kitchen(**extra):
    return {**IP, "raw_hex": "1B4041", "document_type": "kitchen", "job_id": "c-1", **extra}


def test_backup_printer_used_with_banner(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100"})
    result = make(real).print_job(kitchen(fallback_target=BACKUP), None, "x")
    assert result["result"] is True and result["fallback_used"] is True
    label, data = real.calls[0]
    assert label == "192.168.1.60:9100"
    assert b"RESPALDO DE 192.168.1.50:9100" in data and data.endswith(b"\x1b@A")
    assert outbox.counts()["total"] == 0


def test_backup_down_goes_to_queue(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100", "192.168.1.60:9100"})
    result = make(real).print_job(kitchen(fallback_target=BACKUP), None, "x")
    assert result == {"result": True, "status": "queued", "queued": True, "test_mode": False,
                      "message": "La impresora 192.168.1.50:9100 no responde: el trabajo quedó en cola y se "
                                 "imprimirá cuando vuelva."}
    assert outbox.counts()["by_target"] == {"192.168.1.50:9100": 1}
    assert outbox.due()[0].fallback == Target(kind="ip", ip="192.168.1.60", port=9100)


def test_receipt_never_queued(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100"})
    with pytest.raises(BridgeError):
        make(real).print_job({**IP, "raw_hex": "1B40", "document_type": "receipt"}, None, "x")
    assert outbox.counts()["total"] == 0


def test_queue_on_failure_false(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100"})
    with pytest.raises(BridgeError):
        make(real).print_job(kitchen(queue_on_failure=False), None, "x")
    assert outbox.counts()["total"] == 0


def test_config_error_not_queued(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100"}, code=None)
    with pytest.raises(BridgeError):
        make(real).print_job(kitchen(), None, "x")
    assert outbox.counts()["total"] == 0


def test_retry_of_queued_job_is_not_reprinted(env):
    _, outbox, make = env
    real = RoutedBackend(down={"192.168.1.50:9100"})
    service = make(real)
    service.print_job(kitchen(), None, "x")
    real.down.clear()
    again = service.print_job(kitchen(), None, "x")
    assert again["queued"] is True and again["status"] == "queued"
    assert real.calls == []
    assert outbox.counts()["total"] == 1


def test_printed_job_remembered_across_restart(env):
    settings, outbox, make = env
    real = RoutedBackend()
    make(real).print_job(kitchen(), None, "x")
    restarted = make(real)  # mismo outbox en disco, servicio nuevo
    assert restarted.print_job(kitchen(), None, "x")["duplicate"] is True
    assert len(real.calls) == 1


# -- estado ESC/POS de impresoras de red ---------------------------------------
class FakePrinterSocket:
    def __init__(self, replies, log):
        self.replies = list(replies)
        self.log = log

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.log.append(bytes(data))

    def recv(self, size):
        if not self.replies:
            raise socket.timeout("sin respuesta")
        return self.replies.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_backend(replies_by_call):
    log, calls = [], []

    def connect(address, timeout):
        calls.append(address)
        return FakePrinterSocket(replies_by_call.pop(0) if replies_by_call else [], log)

    return WindowsBackend(win32print=FakeWin32Print(), connect=connect), log, calls


NET = Target(kind="ip", ip="192.168.1.50", port=9100)


def test_no_paper_detected_before_sending():
    backend, log, _ = make_backend([[b"\x00", b"\x60"]])
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(NET, b"\x1b@hola", "Dimabe kitchen")
    assert exc.value.code == "printer_no_paper"
    assert b"\x1b@hola" not in log


def test_cover_open_detected():
    backend, _, _ = make_backend([[b"\x04", b"\x00"]])
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(NET, b"x", "Dimabe kitchen")
    assert exc.value.code == "printer_cover_open"


def test_ok_status_then_prints():
    backend, log, _ = make_backend([[b"\x00", b"\x00"]])
    backend.send_raw(NET, b"\x1b@hola", "Dimabe kitchen")
    assert log[-1] == b"\x1b@hola"


def test_silent_printer_prints_and_is_not_asked_again():
    backend, log, _ = make_backend([[], []])
    backend.send_raw(NET, b"uno", "Dimabe kitchen")
    backend.send_raw(NET, b"dos", "Dimabe kitchen")
    assert log.count(b"\x10\x04\x02") == 1
    assert log[-1] == b"dos"


def test_labels_skip_status_query():
    backend, log, _ = make_backend([[]])
    backend.send_raw(NET, b"^XA^XZ", "Dimabe label")
    assert log == [b"^XA^XZ"]
