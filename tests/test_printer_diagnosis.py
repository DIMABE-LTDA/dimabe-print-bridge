from datetime import datetime, timedelta, timezone

import pytest

from bridge.backends import Target, WindowsBackend
from bridge.errors import BridgeError
from bridge.monitor import windows_state
from bridge.panel_api import _with_hints


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
