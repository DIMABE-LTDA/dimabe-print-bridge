"""Vigilante: si el puente deja de responder su propio /api/health, se reinicia."""
from __future__ import annotations

import logging
import threading
import urllib.request
from typing import Callable

logger = logging.getLogger(__name__)


def health_check(port, api_key=lambda: "", timeout: float = 5) -> Callable[[], bool]:
    """Consulta el propio /api/health con el puerto y la clave vigentes."""

    def check() -> bool:
        current_port = port() if callable(port) else port
        key = api_key() if callable(api_key) else api_key
        headers = {"X-Dimabe-Bridge-Key": key} if key else {}
        request = urllib.request.Request(f"http://127.0.0.1:{current_port}/api/health", headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - solo loopback
            return response.status == 200

    return check


class Watchdog:
    def __init__(self, check: Callable[[], bool], on_fail: Callable[[], None], interval: float = 30,
                 failures: int = 3):
        self.check = check
        self.on_fail = on_fail
        self.interval = interval
        self.failures = failures
        self._count = 0
        self._stop = threading.Event()

    def run_once(self) -> None:
        try:
            ok = bool(self.check())
        except Exception as exc:  # noqa: BLE001 - cualquier falla cuenta
            logger.warning("El puente no respondió al vigilante: %s", exc)
            ok = False
        if ok:
            self._count = 0
            return
        self._count += 1
        if self._count >= self.failures:
            logger.error("El puente no responde hace %s revisiones: se reinicia.", self._count)
            self._count = 0
            self.on_fail()

    def start(self, initial_delay: float = 60) -> None:
        def loop():
            if self._stop.wait(initial_delay):
                return
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(self.interval)

        threading.Thread(target=loop, name="watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
