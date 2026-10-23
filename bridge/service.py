"""Servicio de impresión: decide destino y backend, evita duplicados y deja todo en la bitácora."""
from __future__ import annotations

import base64
import binascii
import re
import threading
import time
from typing import Callable

from bridge.backends import Backend, Target
from bridge.config import Settings
from bridge.errors import BridgeError
from bridge.journal import Journal
from bridge.security import validate_network_target

DEDUPE_SECONDS = 300
LOCK_TIMEOUT_S = 10
# Más grande que esto no se guarda en la bitácora (una boleta rasterizada pesa ~200 KB).
MAX_STORED_PAYLOAD = 64 * 1024
MAX_STORED_PAYLOAD_TEST = 2 * 1024 * 1024
_HEX_JUNK = re.compile(r"[^0-9A-Fa-f]")


def _decode_base64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(re.sub(r"\s+", "", text), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BridgeError("The base64 payload is invalid.", 400) from exc


def _decode_hex(value: str) -> bytes:
    text = _HEX_JUNK.sub("", value)
    if len(text) % 2:
        raise BridgeError("The hex payload length must be even.", 400)
    return bytes.fromhex(text)


def _filled(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else ""


class PrintService:
    def __init__(self, settings_provider: Callable[[], Settings], real: Backend, virtual: Backend,
                 journal: Journal, clock: Callable[[], float] = time.monotonic,
                 lock_timeout: float = LOCK_TIMEOUT_S):
        self.settings_provider = settings_provider
        self.lock_timeout = lock_timeout
        self.real = real
        self.virtual = virtual
        self.journal = journal
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._done_jobs: dict[tuple, float] = {}
        self._in_flight: set[tuple] = set()
        self._jobs_guard = threading.Lock()

    # -- destino -------------------------------------------------------
    def parse_target(self, payload: dict) -> Target:
        ip = _filled(payload, "ip_address")
        if ip:
            try:
                port = int(payload.get("ip_port") or 9100)
            except (TypeError, ValueError) as exc:
                raise BridgeError("ip_port must be a number.", 400) from exc
            ip = validate_network_target(ip, port, self.settings_provider().allowed_printer_ports)
            return Target(kind="ip", ip=ip, port=port)
        name = _filled(payload, "printer_name")
        if name:
            name = name.strip()
            if name.startswith("\\\\") or name.startswith("//"):
                # Una ruta UNC haría que Windows se conecte por SMB a cualquier equipo
                # con las credenciales del usuario: solo si ya está instalada.
                try:
                    installed = {p.get("Name") for p in self.real.list_printers()}
                except Exception:  # noqa: BLE001
                    installed = set()
                if name not in installed:
                    raise BridgeError("Esa impresora compartida no está instalada en este equipo.", 400)
            return Target(kind="usb", printer_name=name)
        raise BridgeError("A printer target is required (printer_name for USB or ip_address for network).", 400)

    def _lock_for(self, label: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(label, threading.Lock())

    def is_busy(self, label: str) -> bool:
        with self._locks_guard:
            lock = self._locks.get(label)
        return bool(lock and lock.locked())

    def _backend(self, settings: Settings) -> Backend:
        return self.virtual if settings.test_mode else self.real

    # -- dedupe --------------------------------------------------------
    def _claim(self, key: tuple | None) -> bool:
        """Reserva el trabajo; False si ya se imprimió o se está imprimiendo."""
        if key is None:
            return True
        now = self.clock()
        with self._jobs_guard:
            for old in [k for k, t in self._done_jobs.items() if now - t > DEDUPE_SECONDS]:
                del self._done_jobs[old]
            if key in self._done_jobs or key in self._in_flight:
                return False
            self._in_flight.add(key)
            return True

    def _release(self, key: tuple | None, done: bool) -> None:
        if key is None:
            return
        with self._jobs_guard:
            self._in_flight.discard(key)
            if done:
                self._done_jobs[key] = self.clock()

    # -- ejecución común -----------------------------------------------
    def _run(self, *, kind: str, target: Target, document_type: str, origin, client_ip, source,
             payload_kind: str, data: bytes, job_id: str, action: Callable[[Backend], None],
             success_message: str) -> dict:
        settings = self.settings_provider()
        base = dict(kind=kind, document_type=document_type, target_label=target.label,
                    target_kind=target.kind, printer_alias=target.label,
                    origin=origin if source == "api" else source, client_ip=client_ip,
                    bytes=len(data), job_id=job_id or None, test_mode=settings.test_mode,
                    payload_kind=payload_kind)
        store = settings.test_mode or (
            settings.store_images if payload_kind == "image" else settings.store_payload)
        limit = MAX_STORED_PAYLOAD_TEST if settings.test_mode else MAX_STORED_PAYLOAD
        if payload_kind == "image" and settings.store_images:
            limit = MAX_STORED_PAYLOAD_TEST
        base["payload"] = data if store and payload_kind != "none" and len(data) <= limit else None

        key = (job_id, target.label, bool(settings.test_mode)) if job_id else None
        if not self._claim(key):
            self.journal.record(**base, duration_ms=0, result="duplicate", http_status=200,
                                message="Trabajo repetido: no se volvió a imprimir.")
            return {"result": True, "status": "success", "message": success_message,
                    "test_mode": settings.test_mode, "duplicate": True}

        started = time.monotonic()
        done = False
        try:
            lock = self._lock_for(target.label)
            # Espera acotada: una impresora trabada no puede ocupar todos los hilos.
            if not lock.acquire(timeout=self.lock_timeout):
                raise BridgeError(f"La impresora {target.label} está ocupada con otro trabajo. Intenta de nuevo.",
                                  503, code="printer_busy")
            try:
                action(self._backend(settings))
            finally:
                lock.release()
            done = True
        except BridgeError as exc:
            self.journal.record(**base, duration_ms=round((time.monotonic() - started) * 1000),
                                result="error", http_status=exc.status, message=exc.message)
            raise
        except Exception as exc:
            self.journal.record(**base, duration_ms=round((time.monotonic() - started) * 1000),
                                result="error", http_status=500, message=str(exc))
            raise BridgeError(str(exc), 500) from exc
        finally:
            self._release(key, done)
        self.journal.record(**base, duration_ms=round((time.monotonic() - started) * 1000),
                            result="ok", http_status=200, message=success_message)
        return {"result": True, "status": "success", "message": success_message,
                "test_mode": settings.test_mode}

    # -- API pública ---------------------------------------------------
    def print_job(self, payload: dict, origin: str | None, client_ip: str, source: str = "api") -> dict:
        target = self.parse_target(payload)
        settings = self.settings_provider()
        document_type = _filled(payload, "document_type") or "receipt"
        job_name = f"Dimabe {document_type}"
        job_id = _filled(payload, "job_id")
        try:
            paper = int(payload.get("paper_width_mm") or settings.default_paper_width_mm)
        except (TypeError, ValueError):
            paper = settings.default_paper_width_mm

        if _filled(payload, "raw_base64"):
            data, payload_kind = _decode_base64(payload["raw_base64"]), "raw"
        elif _filled(payload, "raw_hex"):
            data, payload_kind = _decode_hex(payload["raw_hex"]), "raw"
        elif _filled(payload, "receipt"):
            data, payload_kind = _decode_base64(payload["receipt"]), "image"
        else:
            raise BridgeError("receipt is required unless raw_base64 or raw_hex is provided.", 400)

        if payload_kind == "raw":
            action = lambda b: b.send_raw(target, data, job_name)  # noqa: E731
            message = f"Raw bytes sent to '{target.label}'."
        else:
            action = lambda b: b.print_image(target, data, paper, job_name)  # noqa: E731
            message = f"Printed on '{target.label}'."
        return self._run(kind="print", target=target, document_type=document_type, origin=origin,
                         client_ip=client_ip, source=source, payload_kind=payload_kind, data=data,
                         job_id=job_id, action=action, success_message=message)

    def cashbox(self, payload: dict, origin: str | None, client_ip: str, source: str = "api") -> dict:
        target = self.parse_target(payload)
        return self._run(kind="cashbox", target=target, document_type="cashbox", origin=origin,
                         client_ip=client_ip, source=source, payload_kind="none", data=b"",
                         job_id=_filled(payload, "job_id"), action=lambda b: b.open_cashbox(target),
                         success_message=f"Cash drawer pulse sent to '{target.label}'.")

    def reprint(self, event_id: int, target: Target | None = None) -> dict:
        event = self.journal.get(event_id)
        if not event or not event.get("payload"):
            raise BridgeError("Ese registro no tiene los bytes guardados para reenviar.", 404)
        if target is None:
            label = event["target_label"] or ""
            if event["target_kind"] == "ip" and ":" in label:
                ip, port = label.rsplit(":", 1)
                target = Target(kind="ip", ip=ip, port=int(port))
            else:
                target = Target(kind="usb", printer_name=label)
        payload = {"document_type": event["document_type"] or "receipt"}
        if target.kind == "ip":
            payload.update(ip_address=target.ip, ip_port=target.port)
        else:
            payload["printer_name"] = target.printer_name
        if event["payload_kind"] == "image":
            payload["receipt"] = base64.b64encode(bytes.fromhex(event["payload"])).decode()
        else:
            payload["raw_hex"] = event["payload"]
        return self.print_job(payload, None, "127.0.0.1", source="panel")
