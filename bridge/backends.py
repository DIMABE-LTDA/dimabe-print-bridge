"""Salida real a impresoras de Windows: cola RAW, imagen y red (TCP 9100)."""
from __future__ import annotations

import io
import logging
import socket
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from bridge.errors import BridgeError

logger = logging.getLogger(__name__)

CASH_DRAWER_PULSE = bytes([0x1B, 0x70, 0x00, 0x19, 0xFA])
CONNECT_TIMEOUT_S = 3
WRITE_TIMEOUT_S = 5
STATUS_TIMEOUT_S = 0.5
SILENT_STATUS_TTL_S = 3600
DLE_EOT_OFFLINE_CAUSE = b""  # bit 0x04: tapa abierta
DLE_EOT_PAPER = b""  # bits 0x60: sin papel

PRINTER_STATUS_PAUSED = 0x1
PRINTER_STATUS_ERROR = 0x2
PRINTER_STATUS_PAPER_OUT = 0x10
PRINTER_STATUS_OFFLINE = 0x80
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400
STATUS_TEXT = {"paused": "pausada", "offline": "sin conexión", "error": "con error"}


@dataclass(frozen=True)
class Target:
    kind: Literal["usb", "ip"]
    printer_name: str = ""
    ip: str = ""
    port: int = 9100

    @property
    def label(self) -> str:
        return f"{self.ip}:{self.port}" if self.kind == "ip" else self.printer_name


class Backend(Protocol):
    def send_raw(self, target: Target, data: bytes, job_name: str) -> None: ...
    def print_image(self, target: Target, image: bytes, paper_width_mm: int, job_name: str) -> None: ...
    def open_cashbox(self, target: Target) -> None: ...
    def list_printers(self) -> list[dict]: ...
    def queue_status(self, printer_name: str) -> str: ...


def image_pages(image_size: tuple[int, int], printable: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    """Trozos (x, y, ancho, alto) de la imagen ya escalada al ancho imprimible."""
    img_w, img_h = image_size
    page_w, page_h = printable
    scaled_h = max(1, round(img_h * page_w / img_w))
    pages, y = [], 0
    while y < scaled_h:
        height = min(page_h, scaled_h - y)
        pages.append((0, y, page_w, height))
        y += height
    return pages


class WindowsBackend:
    def __init__(self, win32print=None, win32ui=None, connect=socket.create_connection):
        if win32print is None:
            import win32print  # noqa: PLC0415
        self.win32print = win32print
        self._win32ui = win32ui
        self.connect = connect
        self._silent_status: dict[str, float] = {}

    # -- estado --------------------------------------------------------
    def queue_status(self, printer_name: str) -> str:
        try:
            handle = self.win32print.OpenPrinter(printer_name)
        except Exception:  # pywintypes.error
            return "not_found"
        try:
            info = self.win32print.GetPrinter(handle, 2)
        finally:
            self.win32print.ClosePrinter(handle)
        status, attributes = info.get("Status", 0), info.get("Attributes", 0)
        if status & PRINTER_STATUS_PAUSED:
            return "paused"
        if status & PRINTER_STATUS_OFFLINE or attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE:
            return "offline"
        if status & (PRINTER_STATUS_ERROR | PRINTER_STATUS_PAPER_OUT):
            return "error"
        return "ok"

    def _assert_ready(self, printer_name: str) -> None:
        state = self.queue_status(printer_name)
        if state == "not_found":
            raise BridgeError(f"Printer '{printer_name}' was not found in Windows.", 500)
        if state != "ok":
            raise BridgeError(f"La impresora '{printer_name}' está {STATUS_TEXT[state]} en Windows.", 500,
                              code="printer_offline")

    def list_printers(self) -> list[dict]:
        flags = self.win32print.PRINTER_ENUM_LOCAL | self.win32print.PRINTER_ENUM_CONNECTIONS
        printers = self.win32print.EnumPrinters(flags, None, 2)
        return sorted(
            (
                {
                    "Name": p["pPrinterName"],
                    "DriverName": p.get("pDriverName", ""),
                    "PortName": p.get("pPortName", ""),
                    "Type": "Connection" if p.get("pServerName") else "Local",
                    "Shared": bool(p.get("Attributes", 0) & 0x8),
                }
                for p in printers
            ),
            key=lambda p: p["Name"].lower(),
        )

    # -- envío ---------------------------------------------------------
    def send_raw(self, target: Target, data: bytes, job_name: str) -> None:
        if target.kind == "ip":
            ask_status = not job_name.endswith("label") and not data.lstrip().startswith(b"^XA")
            self._send_network(target, data, ask_status=ask_status)
            return
        self._assert_ready(target.printer_name)
        wp = self.win32print
        handle = wp.OpenPrinter(target.printer_name)
        doc_started = False
        try:
            wp.StartDocPrinter(handle, 1, (job_name, None, "RAW"))
            doc_started = True
            wp.StartPagePrinter(handle)
            written = wp.WritePrinter(handle, data)
            if written != len(data):
                wp.AbortPrinter(handle)
                doc_started = False
                raise BridgeError(
                    f"WritePrinter wrote {written} of {len(data)} bytes for '{target.printer_name}'.", 500)
            wp.EndPagePrinter(handle)
        except BridgeError:
            raise
        except Exception as exc:
            if doc_started:
                wp.AbortPrinter(handle)
                doc_started = False
            raise BridgeError(f"No se pudo imprimir en '{target.printer_name}': {exc}", 500) from exc
        finally:
            if doc_started:
                wp.EndDocPrinter(handle)
            wp.ClosePrinter(handle)

    def _ask(self, sock, command: bytes) -> int | None:
        sock.sendall(command)
        try:
            reply = sock.recv(1)
        except (socket.timeout, TimeoutError):
            return None
        return reply[0] if reply else None

    def _check_status(self, sock, target: Target) -> None:
        """Pregunta sin papel / tapa abierta (ESC/POS DLE EOT) antes de mandar el trabajo."""
        silent_since = self._silent_status.get(target.label)
        if silent_since is not None and time.monotonic() - silent_since < SILENT_STATUS_TTL_S:
            return
        sock.settimeout(STATUS_TIMEOUT_S)
        cause = self._ask(sock, DLE_EOT_OFFLINE_CAUSE)
        if cause is None:
            # No informa su estado (o no es ESC/POS): se imprime igual y no se pregunta por 1 h.
            self._silent_status[target.label] = time.monotonic()
            return
        paper = self._ask(sock, DLE_EOT_PAPER)
        if paper is not None and paper & 0x60:
            raise BridgeError(f"La impresora {target.label} no tiene papel.", 500, code="printer_no_paper")
        if cause & 0x04:
            raise BridgeError(f"La impresora {target.label} tiene la tapa abierta.", 500, code="printer_cover_open")

    def _send_network(self, target: Target, data: bytes, ask_status: bool = False) -> None:
        try:
            sock = self.connect((target.ip, target.port), timeout=CONNECT_TIMEOUT_S)
        except OSError as exc:
            # Nada llegó a la impresora: el POS puede reintentar sin duplicar.
            raise BridgeError(f"No hubo conexión con {target.label}: {exc}", 500, code="printer_unreachable") from exc
        try:
            with sock:
                if ask_status:
                    self._check_status(sock, target)
                sock.settimeout(WRITE_TIMEOUT_S)
                sock.sendall(data)
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError(f"Se cortó el envío a {target.label}: {exc}", 500) from exc

    def open_cashbox(self, target: Target) -> None:
        self.send_raw(target, CASH_DRAWER_PULSE, "Dimabe Cash Drawer")

    def print_image(self, target: Target, image: bytes, paper_width_mm: int, job_name: str) -> None:
        if target.kind == "ip":
            raise BridgeError(
                "Image-based printing requires a USB target (printer_name). "
                "Use raw_hex or raw_base64 for network printers.", 400)
        self._assert_ready(target.printer_name)
        from PIL import Image, ImageWin  # noqa: PLC0415

        win32ui = self._win32ui
        if win32ui is None:
            import win32ui  # noqa: PLC0415
        try:
            picture = Image.open(io.BytesIO(image))
            picture.load()
        except Exception as exc:
            raise BridgeError("La imagen del recibo no es válida.", 400) from exc
        picture = picture.convert("RGB")
        dc = win32ui.CreateDC()
        try:
            dc.CreatePrinterDC(target.printer_name)
            # Área que la impresora puede imprimir de verdad (HORZRES/VERTRES):
            # dibujar al ancho nominal del papel cortaba el borde derecho.
            printable = (dc.GetDeviceCaps(8), dc.GetDeviceCaps(10))
            scaled_w = printable[0]
            scaled_h = max(1, round(picture.height * scaled_w / picture.width))
            scaled = picture.resize((scaled_w, scaled_h), Image.LANCZOS)
            dc.StartDoc(job_name)
            for x, y, w, h in image_pages(picture.size, printable):
                dc.StartPage()
                ImageWin.Dib(scaled.crop((x, y, x + w, y + h))).draw(dc.GetHandleOutput(), (0, 0, w, h))
                dc.EndPage()
            dc.EndDoc()
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"No se pudo imprimir la imagen en '{target.printer_name}': {exc}", 500) from exc
        finally:
            dc.DeleteDC()
