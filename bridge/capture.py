"""Modo prueba: impresoras virtuales que guardan los trabajos y simulan fallas."""
from __future__ import annotations

import itertools
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from bridge.backends import Target
from bridge.config import Settings
from bridge.errors import BridgeError

SIMULATED_ERRORS = {
    "no_paper": "Sin papel (simulado).",
    "offline": "Impresora desconectada (simulado).",
}


@dataclass
class CapturedJob:
    id: int
    ts: str
    target_label: str
    document_type: str
    kind: Literal["raw", "image", "cashbox"]
    data: bytes


class CaptureStore:
    def __init__(self, max_items: int = 500, max_bytes: int = 50 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._jobs: deque[CapturedJob] = deque(maxlen=max_items)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, target_label: str, document_type: str, kind: str, data: bytes) -> CapturedJob:
        with self._lock:
            job = CapturedJob(next(self._ids), datetime.now().astimezone().isoformat(timespec="seconds"),
                              target_label, document_type, kind, bytes(data))
            self._jobs.append(job)
            while len(self._jobs) > 1 and sum(len(j.data) for j in self._jobs) > self.max_bytes:
                self._jobs.popleft()
            return job

    def list(self, target_label: str | None = None) -> list[CapturedJob]:
        with self._lock:
            jobs = [j for j in self._jobs if target_label is None or j.target_label == target_label]
        return list(reversed(jobs))

    def get(self, job_id: int) -> CapturedJob | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()

    def targets(self) -> dict[str, int]:
        with self._lock:
            return dict(Counter(j.target_label for j in self._jobs))


def _document_type(job_name: str) -> str:
    return job_name.removeprefix("Dimabe ").strip() or "receipt"


class VirtualBackend:
    def __init__(self, store: CaptureStore, settings_provider: Callable[[], Settings], sleep=time.sleep):
        self.store = store
        self.settings_provider = settings_provider
        self.sleep = sleep
        self._attempts = 0
        self._lock = threading.Lock()

    def _simulate(self) -> None:
        s = self.settings_provider()
        if s.test_delay_s:
            self.sleep(s.test_delay_s)
        mode = s.test_response
        if mode == "timeout":
            raise BridgeError("Tiempo de espera agotado (simulado).", 500)
        if mode in SIMULATED_ERRORS:
            raise BridgeError(SIMULATED_ERRORS[mode], 500)
        if mode == "one_in_three":
            with self._lock:
                self._attempts += 1
                fail = self._attempts % 3 == 0
            if fail:
                raise BridgeError("Falla simulada (1 de cada 3).", 500)

    def send_raw(self, target: Target, data: bytes, job_name: str) -> None:
        self._simulate()
        self.store.add(target.label, _document_type(job_name), "raw", data)

    def print_image(self, target: Target, image: bytes, paper_width_mm: int, job_name: str) -> None:
        self._simulate()
        self.store.add(target.label, _document_type(job_name), "image", image)

    def open_cashbox(self, target: Target) -> None:
        self._simulate()
        self.store.add(target.label, "cashbox", "cashbox", b"")

    def list_printers(self) -> list[dict]:
        return [
            {"Name": name, "DriverName": "Virtual", "PortName": f"virtual://{name}", "Type": "Virtual", "Shared": False}
            for name in sorted(self.store.targets())
        ]

    def queue_status(self, printer_name: str) -> str:
        return "ok"
