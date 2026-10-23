"""Revisa en segundo plano el estado de las impresoras que usa el POS."""
from __future__ import annotations

import logging
import socket
import threading
from datetime import date
from typing import Callable

logger = logging.getLogger(__name__)

QUEUE_TEXT = {
    "ok": ("ok", "Lista"),
    "paused": ("warn", "Pausada en Windows"),
    "offline": ("warn", "Sin conexión en Windows"),
    "error": ("error", "Con error en Windows"),
    "not_found": ("error", "No existe en Windows"),
}


class PrinterMonitor:
    def __init__(self, real, journal, settings_provider: Callable, interval_s: int = 60,
                 connect=socket.create_connection, is_busy: Callable[[str], bool] = lambda label: False):
        self.is_busy = is_busy
        self.real = real
        self.journal = journal
        self.settings_provider = settings_provider
        self.interval_s = interval_s
        self.connect = connect
        self._status: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="printer-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_now()
            except Exception:  # noqa: BLE001 - el monitor nunca debe caerse
                logger.exception("Falló la revisión de impresoras")
            self._stop.wait(self.interval_s)

    def _labels(self) -> dict[str, str]:
        labels = {label: "ip" for label in self.journal.recent_ip_targets(7)}
        for label, info in self.journal.summary_day(date.today())["by_target"].items():
            labels.setdefault(label, "ip" if ":" in label and label.split(":")[0].count(".") == 3 else "usb")
        return labels

    def _probe(self, label: str, kind: str) -> tuple[str, str]:
        if self.settings_provider().test_mode:
            return "test", "Virtual (modo prueba)"
        if kind == "ip":
            host, _, port = label.rpartition(":")
            try:
                with self.connect((host, int(port)), timeout=1):
                    pass
                return "ok", "Responde"
            except (OSError, ValueError):
                return "error", "No responde"
        try:
            return QUEUE_TEXT.get(self.real.queue_status(label), ("off", "Desconocido"))
        except Exception:  # noqa: BLE001
            return "off", "Desconocido"

    def refresh_now(self) -> None:
        with self._lock:
            previous = dict(self._status)
        status = {}
        for label, kind in self._labels().items():
            # Muchas impresoras de red aceptan una sola conexión: no se toca la que imprime.
            if self.is_busy(label) and label in previous:
                status[label] = previous[label]
            elif self.is_busy(label):
                status[label] = ("ok", "Imprimiendo")
            else:
                status[label] = self._probe(label, kind)
        with self._lock:
            self._status = status

    def snapshot(self) -> list[dict]:
        by_target = self.journal.summary_day(date.today())["by_target"]
        with self._lock:
            status = dict(self._status)
        labels = self._labels()
        rows = []
        for label in sorted(set(labels) | set(status)):
            if self.settings_provider().test_mode:
                state, text = "test", "Virtual (modo prueba)"
            else:
                state, text = status.get(label, ("off", "Sin revisar"))
            info = by_target.get(label, {"ok": 0, "error": 0, "last_ts": None, "last_message": None})
            rows.append({
                "label": label, "kind": labels.get(label, "usb"), "status": state, "status_text": text,
                "today_ok": info["ok"], "today_error": info["error"],
                "last_ts": info["last_ts"], "last_message": info["last_message"],
            })
        return rows
