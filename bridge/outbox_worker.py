"""Reintenta en segundo plano los trabajos que quedaron en la cola."""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)


class OutboxWorker:
    def __init__(self, outbox, service, settings_provider: Callable, interval: float = 2):
        self.outbox = outbox
        self.service = service
        self.settings_provider = settings_provider
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="outbox-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - el trabajador nunca debe morir
                logger.exception("Falló una vuelta de la cola")
            self._stop.wait(self.interval)

    def run_once(self) -> None:
        test_mode = bool(self.settings_provider().test_mode)
        for job in self.outbox.due():
            printed, message = self.service.deliver_queued(job)
            if printed:
                self.outbox.mark_done(job.id, test_mode=test_mode)
            else:
                self.outbox.mark_failed(job.id, message)
