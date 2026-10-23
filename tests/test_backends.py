import socket
import threading

import pytest

from bridge.backends import Target, WindowsBackend, image_pages
from bridge.errors import BridgeError


class FakeWin32Print:
    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    def __init__(self, status=0, attributes=0, short_write=False, printers=("BIXOLON SRP-275III",)):
        self.status = status
        self.attributes = attributes
        self.short_write = short_write
        self.printers = printers
        self.calls = []
        self.written = b""

    def OpenPrinter(self, name):
        if name not in self.printers:
            raise OSError(1801, "OpenPrinter", "El nombre de la impresora no es válido.")
        self.calls.append(("open", name))
        return "h"

    def GetPrinter(self, handle, level):
        return {"Status": self.status, "Attributes": self.attributes}

    def StartDocPrinter(self, handle, level, info):
        self.calls.append(("startdoc", info))
        return 1

    def StartPagePrinter(self, handle):
        self.calls.append(("startpage",))

    def WritePrinter(self, handle, data):
        self.written += data
        return len(data) - 1 if self.short_write else len(data)

    def EndPagePrinter(self, handle):
        self.calls.append(("endpage",))

    def EndDocPrinter(self, handle):
        self.calls.append(("enddoc",))

    def AbortPrinter(self, handle):
        self.calls.append(("abort",))

    def ClosePrinter(self, handle):
        self.calls.append(("close",))

    def EnumPrinters(self, flags, name, level):
        return [{"pPrinterName": p, "pDriverName": "drv", "pPortName": "USB002", "Attributes": 0} for p in self.printers]


USB = Target(kind="usb", printer_name="BIXOLON SRP-275III")


def test_target_labels():
    assert USB.label == "BIXOLON SRP-275III"
    assert Target(kind="ip", ip="192.168.1.50", port=9100).label == "192.168.1.50:9100"


def test_raw_usb_writes_exact_bytes_and_closes():
    fake = FakeWin32Print()
    WindowsBackend(win32print=fake).send_raw(USB, b"\x1b@hola", "Dimabe receipt")
    assert fake.written == b"\x1b@hola"
    assert ("startdoc", ("Dimabe receipt", None, "RAW")) in fake.calls
    assert fake.calls[-3:] == [("endpage",), ("enddoc",), ("close",)]


@pytest.mark.parametrize("status,attributes,state", [(0x1, 0, "paused"), (0x80, 0, "offline"), (0, 0x400, "offline"), (0x10, 0, "error"), (0x2, 0, "error")])
def test_queue_problem_blocks_printing(status, attributes, state):
    fake = FakeWin32Print(status=status, attributes=attributes)
    backend = WindowsBackend(win32print=fake)
    assert backend.queue_status("BIXOLON SRP-275III") == state
    with pytest.raises(BridgeError):
        backend.send_raw(USB, b"x", "job")
    assert fake.written == b""


def test_unknown_printer():
    backend = WindowsBackend(win32print=FakeWin32Print())
    assert backend.queue_status("No existe") == "not_found"
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(Target(kind="usb", printer_name="No existe"), b"x", "job")
    assert "No existe" in exc.value.message


def test_short_write_aborts():
    fake = FakeWin32Print(short_write=True)
    with pytest.raises(BridgeError):
        WindowsBackend(win32print=fake).send_raw(USB, b"abc", "job")
    assert ("abort",) in fake.calls
    assert ("close",) in fake.calls


def test_raw_network_sends_bytes():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received = []

    def accept():
        conn, _ = server.accept()
        with conn:
            chunks = b""
            while chunk := conn.recv(1024):
                chunks += chunk
            received.append(chunks)

    thread = threading.Thread(target=accept)
    thread.start()
    port = server.getsockname()[1]
    WindowsBackend(win32print=FakeWin32Print()).send_raw(Target(kind="ip", ip="127.0.0.1", port=port), b"\x1b@red", "job")
    thread.join(timeout=5)
    server.close()
    assert received == [b"\x1b@red"]


def test_network_connection_error_is_bridge_error():
    def refuse(address, timeout):
        raise ConnectionRefusedError("refused")

    backend = WindowsBackend(win32print=FakeWin32Print(), connect=refuse)
    with pytest.raises(BridgeError) as exc:
        backend.send_raw(Target(kind="ip", ip="192.168.1.50", port=9100), b"x", "job")
    assert "192.168.1.50:9100" in exc.value.message


def test_image_to_ip_rejected():
    with pytest.raises(BridgeError) as exc:
        WindowsBackend(win32print=FakeWin32Print()).print_image(Target(kind="ip", ip="192.168.1.50"), b"img", 80, "job")
    assert exc.value.status == 400


def test_list_printers_shape():
    printers = WindowsBackend(win32print=FakeWin32Print()).list_printers()
    assert printers == [{"Name": "BIXOLON SRP-275III", "DriverName": "drv", "PortName": "USB002", "Type": "Local", "Shared": False}]


def test_image_pages_scale_to_printable_width_and_paginate():
    pages = image_pages(image_size=(1728, 6000), printable=(576, 1000))
    assert pages[0] == (0, 0, 576, 1000)
    total = sum(h for _, _, _, h in pages)
    assert total == 2000
    assert len(pages) == 2


def test_image_pages_short_receipt_single_page():
    assert image_pages(image_size=(576, 300), printable=(576, 3000)) == [(0, 0, 576, 300)]
