import contextlib
from datetime import datetime, timedelta, timezone

import pytest

from bridge.backends import Target, WindowsBackend
from bridge.errors import BridgeError
from bridge.monitor import windows_state
from bridge.panel_api import _diagnose_printer, _with_hints


class FakeSpooler:
    def __init__(self, jobs=(), missing=False, enum_error=False):
        self.jobs, self.missing, self.enum_error = jobs, missing, enum_error

    def OpenPrinter(self, name):
        if self.missing:
            raise OSError("no existe")
        return 1

    def ClosePrinter(self, handle):
        pass

    def GetPrinter(self, handle, level):
        return {"Status": 0, "Attributes": 0}

    def EnumJobs(self, handle, first, count, level):
        if self.enum_error:
            raise OSError("sin acceso")
        return self.jobs


def ago(seconds, tz=timezone.utc):
    return datetime.now(timezone.utc).replace(tzinfo=tz) - timedelta(seconds=seconds)


@pytest.mark.parametrize("job,stuck", [
    ({"Status": 0, "Submitted": ago(5)}, False),
    ({"Status": 0, "Submitted": ago(600)}, True),
    ({"Status": 0, "Submitted": ago(600, tz=None)}, True),
    ({"Status": 0x2, "Submitted": ago(5)}, True),
    ({"Status": 0x1, "Submitted": ago(600)}, False),
    ({"Status": 0}, False),
])
def test_stuck_jobs_detects_undelivered_jobs(job, stuck):
    assert WindowsBackend(win32print=FakeSpooler(jobs=(job,))).stuck_jobs("PRECUENTAS") == int(stuck)


@pytest.mark.parametrize("spooler", [FakeSpooler(missing=True), FakeSpooler(enum_error=True)])
def test_stuck_jobs_never_raises(spooler):
    assert WindowsBackend(win32print=spooler).stuck_jobs("PRECUENTAS") == 0


def test_missing_printer_error_says_what_to_do():
    backend = WindowsBackend(win32print=FakeSpooler(missing=True))
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(Target(kind="usb", printer_name="PRECUENTAS"), b"x", "job")
    assert exc.value.code == "printer_not_found"
    assert "'PRECUENTAS' no existe en este equipo" in exc.value.message
    assert "panel del puente" in exc.value.message


def test_windows_state_reports_stuck_job():
    backend = WindowsBackend(win32print=FakeSpooler(jobs=({"Status": 0x2},)))
    assert windows_state(backend, "PRECUENTAS") == ("warn", "Trabajo atascado en Windows")


def test_windows_state_without_jobs_is_ready():
    assert windows_state(WindowsBackend(win32print=FakeSpooler()), "PRECUENTAS") == ("ok", "Lista")


class RemoteSpooler(FakeSpooler):
    JOB_CONTROL_DELETE = 5

    def __init__(self, port="\\\\CAJA\\PRECUENTAS", rounds=(), cancel_error=False):
        super().__init__()
        self.port, self.rounds, self.cancel_error = port, list(rounds), cancel_error
        self.deleted, self.written = [], b""

    def GetPrinter(self, handle, level):
        return {"Status": 0, "Attributes": 0, "pPortName": self.port}

    def EnumJobs(self, handle, first, count, level):
        return self.rounds.pop(0) if self.rounds else ()

    def SetJob(self, handle, job_id, level, info, command):
        if self.cancel_error:
            raise OSError("sin permiso")
        self.deleted.append((job_id, command))

    def StartDocPrinter(self, handle, level, info):
        return 1

    def StartPagePrinter(self, handle):
        pass

    def WritePrinter(self, handle, data):
        self.written += data
        return len(data)

    def EndPagePrinter(self, handle):
        pass

    def EndDocPrinter(self, handle):
        pass


REMOTE = Target(kind="usb", printer_name="PRECUENTAS")


def job(status=0, document="job", job_id=7):
    return {"JobId": job_id, "Status": status, "pDocument": document}


@pytest.fixture
def fast_delivery(monkeypatch):
    monkeypatch.setattr("bridge.backends.DELIVERY_POLL_S", 0)


def test_delivered_job_on_other_machine_returns(fast_delivery):
    spooler = RemoteSpooler(rounds=[(job(),), ()])
    WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert spooler.written == b"x" and spooler.rounds == [] and spooler.deleted == []


def test_undelivered_job_is_cancelled_and_reported(fast_delivery):
    spooler = RemoteSpooler(rounds=[(job(0x2),)])
    with pytest.raises(BridgeError) as exc:
        WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert exc.value.code == "printer_undelivered"
    assert "no saldrá después" in exc.value.message
    assert spooler.deleted == [(7, 5)]


def test_undelivered_job_that_cannot_be_cancelled_warns_it_may_print(fast_delivery):
    spooler = RemoteSpooler(rounds=[(job(0x2),)], cancel_error=True)
    with pytest.raises(BridgeError) as exc:
        WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert "podría salir más tarde" in exc.value.message


def test_slow_job_is_left_to_the_monitor(monkeypatch, fast_delivery):
    monkeypatch.setattr("bridge.backends.DELIVERY_WAIT_S", 0)
    spooler = RemoteSpooler(rounds=[(job(),)])
    WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert spooler.deleted == []


def test_other_documents_are_not_touched(fast_delivery):
    spooler = RemoteSpooler(rounds=[(job(0x2, document="otro"),)])
    WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert spooler.deleted == []


def test_local_port_does_not_wait_for_delivery():
    spooler = RemoteSpooler(port="USB002", rounds=[(job(0x2),)])
    WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert spooler.rounds and spooler.deleted == []


def test_review_failure_does_not_turn_a_sent_job_into_an_error(fast_delivery):
    spooler = RemoteSpooler()
    spooler.EnumJobs = lambda *args: (_ for _ in ()).throw(OSError("sin acceso"))
    WindowsBackend(win32print=spooler).send_raw(REMOTE, b"x", "job")
    assert spooler.written == b"x"


class DiagnosedBackend:
    def __init__(self, printers, state="ok", stuck=0):
        self.printers, self.state, self.stuck = printers, state, stuck
        self.opened = []

    def list_printers(self):
        return self.printers

    def queue_status(self, name):
        self.opened.append(name)
        return self.state

    def stuck_jobs(self, name):
        return self.stuck


@contextlib.contextmanager
def reachable(address, timeout):
    yield None


def unreachable(address, timeout):
    raise OSError("sin respuesta")


LOCAL_PORT = {"Name": "PRECUENTAS", "DriverName": "BIXOLON", "PortName": "\\\\CAJA\\PRECUENTAS", "Type": "Local"}


def verdicts(checks):
    return {c["title"]: c["ok"] for c in checks}


def test_diagnose_remote_printer_ready_still_asks_to_print():
    checks = _diagnose_printer(DiagnosedBackend([LOCAL_PORT]), "PRECUENTAS", connect=reachable)
    assert verdicts(checks) == {"Instalada en este equipo": True, "Estado en Windows": True,
                                "Red hacia el otro equipo": True, "Permiso para imprimir": None}


def test_diagnose_reports_unreachable_machine():
    checks = _diagnose_printer(DiagnosedBackend([LOCAL_PORT]), "PRECUENTAS", connect=unreachable)
    assert verdicts(checks)["Red hacia el otro equipo"] is False
    assert any("CAJA no responde" in c["detail"] for c in checks)


def test_diagnose_reports_stuck_job():
    checks = _diagnose_printer(DiagnosedBackend([LOCAL_PORT], stuck=1), "PRECUENTAS", connect=reachable)
    assert verdicts(checks)["Estado en Windows"] is False


def test_diagnose_reports_shared_connection():
    connection = {"Name": "\\\\CAJA\\PRECUENTAS", "PortName": "USB001", "Type": "Connection"}
    checks = _diagnose_printer(DiagnosedBackend([connection]), "\\\\CAJA\\PRECUENTAS", connect=reachable)
    assert verdicts(checks)["Tipo de instalación"] is False


def test_diagnose_never_opens_a_printer_that_is_not_installed():
    backend = DiagnosedBackend([{"Name": "\\\\CAJA\\PRECUENTAS", "PortName": "USB001", "Type": "Connection"}])
    called = []
    checks = _diagnose_printer(backend, "\\\\OTRO\\X", connect=lambda *a, **k: called.append(a))
    assert verdicts(checks) == {"Instalada en este equipo": False}
    assert backend.opened == [] and called == []


def test_diagnose_missing_printer_finds_similar_name():
    backend = DiagnosedBackend([{"Name": "\\\\CAJA\\PRECUENTAS", "PortName": "USB001", "Type": "Connection"}])
    checks = _diagnose_printer(backend, "PRECUENTAS", connect=reachable)
    assert "\\\\CAJA\\PRECUENTAS" in checks[0]["detail"]


def missing(label="PRECUENTAS"):
    return {"label": label, "status": "error", "status_text": "No existe en Windows"}


@pytest.mark.parametrize("windows,expected", [
    ([{"Name": "Fax"}], "Este equipo no tiene esa impresora"),
    ([], "Este equipo no tiene esa impresora"),
    ([{"Name": "\\\\CAJA\\PRECUENTAS"}], "En Windows está como «\\\\CAJA\\PRECUENTAS»"),
    ([{"Name": "PRECUENTAS "}], "En Windows está como «PRECUENTAS »"),
])
def test_hint_explains_missing_printer(windows, expected):
    assert expected in _with_hints([missing()], windows)[0]["hint"]


def test_hint_only_for_missing_printers():
    ready = {"label": "Cocina", "status": "ok", "status_text": "Lista"}
    assert "hint" not in _with_hints([ready], [])[0]
