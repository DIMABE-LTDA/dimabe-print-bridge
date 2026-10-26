"""Ícono en la bandeja de Windows: color según estado y menú para TI.

Funciona igual sobre el puente en el mismo proceso (modo todo en uno) o sobre
el servicio de Windows por HTTP (modo `--tray`): ambos se exponen con la
misma interfaz `TrayBackend`.
"""
from __future__ import annotations

import logging
import os
import threading
import webbrowser
from typing import Callable

from bridge import APP_NAME, __version__
from bridge.config import data_dir

logger = logging.getLogger(__name__)

COLORS = {
    "ok": (31, 92, 70),        # verde: en línea
    "warn": (154, 91, 0),      # ámbar: impresora con errores o cola acumulada
    "test": (91, 63, 168),     # morado: modo prueba
    "down": (107, 117, 110),   # gris: el puente no responde o no pudo abrir el puerto
}
SUMMARY = {"ok": "En línea", "warn": "En línea · revisar impresoras", "test": "Modo prueba",
           "down": "Sin servicio"}


def make_icon(state: str, size: int = 64):
    """Impresora estilizada sobre un cuadrado del color del estado."""
    from PIL import Image, ImageDraw  # noqa: PLC0415

    scale = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(12 * scale), fill=COLORS[state] + (255,))
    s = lambda *v: [int(x * scale) for x in v]  # noqa: E731
    white = (255, 255, 255, 255)
    d.rectangle(s(20, 12, 44, 24), outline=white, width=max(2, int(4 * scale)))
    d.rounded_rectangle(s(12, 24, 52, 42), radius=int(4 * scale), outline=white, width=max(2, int(4 * scale)))
    d.rectangle(s(20, 36, 44, 52), fill=COLORS[state] + (255,), outline=white, width=max(2, int(4 * scale)))
    return img


class TrayBackend:
    """Lo que la bandeja necesita saber y poder hacer."""

    port: int

    def state(self) -> tuple[str, str]:  # (estado, detalle)
        raise NotImplementedError

    def test_mode(self) -> bool:
        raise NotImplementedError

    def set_test_mode(self, enabled: bool) -> None:
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError

    def print_test(self) -> None:
        raise NotImplementedError


class LocalTrayBackend(TrayBackend):
    def __init__(self, ctx, print_test: Callable[[], None]):
        self.ctx = ctx
        self._print_test = print_test

    @property
    def port(self) -> int:
        return self.ctx.settings.port

    def state(self) -> tuple[str, str]:
        runner = self.ctx.runner
        if runner is not None and runner.state != "running":
            owner = getattr(runner, "busy_by", "") or "otro programa"
            return "down", f"El puerto {self.port} lo está usando {owner}."
        if self.ctx.settings.test_mode:
            return "test", ""
        queued = self.ctx.outbox.counts()["total"] if self.ctx.outbox is not None else 0
        if self.ctx.journal.recent_errors(30) or queued:
            return "warn", f"{queued} comanda(s) en cola." if queued else "Impresora con errores."
        return "ok", ""

    def test_mode(self) -> bool:
        return self.ctx.settings.test_mode

    def set_test_mode(self, enabled: bool) -> None:
        self.ctx.settings.test_mode = enabled
        self.ctx.save(self.ctx.settings)
        self.ctx.journal.record(kind="config", origin="bandeja", result="ok",
                                message="Modo prueba " + ("encendido" if enabled else "apagado"), test_mode=enabled)

    def restart(self) -> None:
        if self.ctx.runner is not None:
            self.ctx.runner.restart()

    def print_test(self) -> None:
        self._print_test()


class HttpTrayBackend(TrayBackend):
    """La bandeja de un usuario frente al servicio de Windows."""

    def __init__(self, client):
        self.client = client
        self.port = client.port
        self._last: dict | None = None

    def _status(self) -> dict | None:
        self._last = self.client.status()
        return self._last

    def state(self) -> tuple[str, str]:
        status = self._status()
        if status is None:
            return "down", "El servicio Dimabe Print Bridge no responde."
        if status["service"].get("test_mode"):
            return "test", ""
        levels = {a.get("level") for a in status.get("attention", [])}
        if "error" in levels or status.get("today", {}).get("error"):
            titles = [a["title"] for a in status["attention"] if a.get("level") == "error"]
            return "warn", titles[0] if titles else "Impresora con errores."
        return "ok", ""

    def test_mode(self) -> bool:
        return bool(self._last and self._last["service"].get("test_mode"))

    def set_test_mode(self, enabled: bool) -> None:
        self.client.set_test_mode(enabled)

    def restart(self) -> None:
        self.client.restart()

    def print_test(self) -> None:
        self.client.print_test()


def run_tray(backend: TrayBackend, on_quit: Callable[[], None], quit_label: str = "Salir") -> bool:
    try:
        import pystray  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.info("Sin ícono de bandeja (pystray no disponible)")
        return False

    url = lambda: f"http://127.0.0.1:{backend.port}/panel"  # noqa: E731
    current = {"state": "ok", "detail": ""}

    def summary(_item=None) -> str:
        return f"{APP_NAME} {__version__} · {SUMMARY[current['state']]}"

    def refresh(icon) -> None:
        state, detail = backend.state()
        changed = state != current["state"]
        current.update(state=state, detail=detail)
        icon.icon = make_icon(state)
        icon.title = summary()[:127]
        icon.update_menu()
        if changed and state in ("down", "warn") and detail and getattr(icon, "HAS_NOTIFICATION", False):
            icon.notify(detail, APP_NAME)

    def safe(action):
        def handler(icon, _item):
            try:
                action()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Acción de bandeja falló: %s", exc)
                if getattr(icon, "HAS_NOTIFICATION", False):
                    icon.notify(f"No se pudo: {exc}", APP_NAME)
            refresh(icon)
        return handler

    def quit_app(icon, _item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(summary, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Abrir panel", lambda i, _: webbrowser.open(url()), default=True),
        pystray.MenuItem("Imprimir ticket de prueba", safe(backend.print_test)),
        pystray.MenuItem("Modo prueba", safe(lambda: backend.set_test_mode(not backend.test_mode())),
                         checked=lambda _i: backend.test_mode()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Abrir carpeta de bitácora", lambda i, _: os.startfile(data_dir())),
        pystray.MenuItem("Reiniciar servicio", safe(backend.restart)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(quit_label, quit_app),
    )
    icon = pystray.Icon("DimabePrintBridge", make_icon("ok"), summary()[:127], menu)

    def watcher():
        while True:
            try:
                refresh(icon)
            except Exception:  # noqa: BLE001
                logger.exception("Error actualizando el ícono")
            threading.Event().wait(10)

    threading.Thread(target=watcher, daemon=True).start()
    icon.run()
    return True
