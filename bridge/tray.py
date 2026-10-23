"""Ícono en la bandeja de Windows: color según estado y menú para TI."""
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
    "warn": (154, 91, 0),      # ámbar: alguna impresora con errores recientes
    "test": (91, 63, 168),     # morado: modo prueba
    "down": (107, 117, 110),   # gris: el puerto no se pudo abrir
}


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


def tray_state(ctx) -> str:
    runner = ctx.runner
    if runner is not None and runner.state != "running":
        return "down"
    if ctx.settings.test_mode:
        return "test"
    if ctx.journal.recent_errors(30):
        return "warn"
    return "ok"


def run_tray(ctx, on_quit: Callable[[], None], on_print_test: Callable[[], None]) -> bool:
    try:
        import pystray  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.info("Sin ícono de bandeja (pystray no disponible)")
        return False

    url = lambda: f"http://127.0.0.1:{ctx.settings.port}/panel"  # noqa: E731

    def summary(_item=None) -> str:
        state = tray_state(ctx)
        text = {"ok": "En línea", "warn": "En línea · impresora con errores", "test": "Modo prueba",
                "down": "Puerto ocupado"}[state]
        return f"{APP_NAME} {__version__} · {text}"

    def toggle_test(icon, _item):
        ctx.settings.test_mode = not ctx.settings.test_mode
        ctx.save(ctx.settings)
        ctx.journal.record(kind="config", origin="bandeja", result="ok",
                           message="Modo prueba " + ("encendido" if ctx.settings.test_mode else "apagado"),
                           test_mode=ctx.settings.test_mode)
        refresh(icon)

    def refresh(icon):
        state = tray_state(ctx)
        icon.icon = make_icon(state)
        icon.title = summary()[:127]
        icon.update_menu()

    def quit_app(icon, _item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(summary, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Abrir panel", lambda i, _: webbrowser.open(url()), default=True),
        pystray.MenuItem("Imprimir ticket de prueba", lambda i, _: on_print_test()),
        pystray.MenuItem("Modo prueba", toggle_test, checked=lambda _i: ctx.settings.test_mode),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Abrir carpeta de bitácora", lambda i, _: os.startfile(data_dir())),
        pystray.MenuItem("Reiniciar servicio", lambda i, _: ctx.runner and ctx.runner.restart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Salir", quit_app),
    )
    icon = pystray.Icon("DimabePrintBridge", make_icon(tray_state(ctx)), summary()[:127], menu)

    def watcher():
        last = None
        notified_busy = False
        while True:
            threading.Event().wait(5)
            try:
                state = tray_state(ctx)
                if state != last:
                    refresh(icon)
                    last = state
                if state == "down" and not notified_busy and getattr(icon, "HAS_NOTIFICATION", False):
                    owner = getattr(ctx.runner, "busy_by", "") or "otro programa"
                    icon.notify(f"El puerto {ctx.settings.port} lo está usando {owner}. "
                                "Ciérralo para que el POS pueda imprimir.", APP_NAME)
                    notified_busy = True
                if state != "down":
                    notified_busy = False
            except Exception:  # noqa: BLE001
                logger.exception("Error actualizando el ícono")

    threading.Thread(target=watcher, daemon=True).start()
    icon.run()
    return True
