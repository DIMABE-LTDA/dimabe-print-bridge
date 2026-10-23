"""Punto de entrada de Dimabe Print Bridge."""
from __future__ import annotations

import argparse
import logging
import os
import logging.handlers
import random
import sys
import threading
from datetime import datetime, timedelta

from bridge import APP_NAME, __version__
from bridge.config import apply_installer_seed, load_settings, log_dir, save_settings, seed_path

logger = logging.getLogger("bridge")


def setup_logging() -> None:
    handler = logging.handlers.RotatingFileHandler(log_dir() / "bridge.log", maxBytes=2 * 1024 * 1024,
                                                   backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:
        root.addHandler(logging.StreamHandler())


def single_instance(name: str = "Local\\DimabePrintBridge") -> object | None:
    """Mutex de Windows: una sola instancia por sesión."""
    try:
        import win32api  # noqa: PLC0415
        import win32event  # noqa: PLC0415
        import winerror  # noqa: PLC0415
    except ImportError:
        return object()
    mutex = win32event.CreateMutex(None, False, name)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return None
    return mutex


def load_demo(ctx) -> None:
    """Datos ficticios del día para mostrar el panel sin POS (--demo)."""
    from bridge.samples import SAMPLES  # noqa: PLC0415
    journal = ctx.journal
    real_clock = journal.clock
    targets = [("Boletas", "usb", "receipt"), ("192.168.50.11:9100", "ip", "kitchen"),
               ("192.168.50.12:9100", "ip", "kitchen"), ("Etiquetas", "usb", "label")]
    start = datetime.now().replace(hour=9, minute=0, second=0)
    rng = random.Random(23)
    for i in range(400):
        moment = start + timedelta(minutes=rng.randint(0, 6 * 60))
        if moment > datetime.now():
            continue
        label, kind, doc = rng.choice(targets)
        failed = label.startswith("192.168.50.12") and rng.random() < 0.05
        journal.clock = lambda m=moment: m
        journal.record(kind="print", document_type=doc, target_label=label, target_kind=kind,
                       origin="https://dimabe-odoo-grupo-los-lirios.odoo.com", client_ip="127.0.0.1",
                       bytes=rng.randint(400, 52000), duration_ms=rng.randint(60, 900),
                       result="error" if failed else "ok", http_status=500 if failed else 200,
                       message=f"No hubo conexión con {label}" if failed else f"Raw bytes sent to '{label}'.",
                       payload_kind="raw", payload=SAMPLES["comanda"][1]() if doc == "kitchen" else None)
    journal.clock = real_clock
    journal.flush()


class Core:
    """El puente sin bandeja: API, panel, cola, monitor, actualizador y vigilante."""

    def __init__(self, ctx, parts, stop):
        self.ctx = ctx
        self.parts = parts
        self.stop = stop

    def shutdown(self) -> None:
        self.stop.set()
        for part in self.parts:
            try:
                part.stop()
            except Exception:  # noqa: BLE001
                logger.exception("Error deteniendo %s", part)
        self.ctx.journal.record(kind="service", result="ok", message="Servicio detenido")
        self.ctx.journal.stop()


def start_core(mode: str = "all", port: int | None = None, demo: bool = False) -> Core:
    """Arranca el núcleo. mode: "all" (con bandeja aparte), "core" o "service"."""
    from bridge.monitor import PrinterMonitor  # noqa: PLC0415
    from bridge.outbox_worker import OutboxWorker  # noqa: PLC0415
    from bridge.server import ServerRunner, build_context  # noqa: PLC0415
    from bridge.updater import Updater  # noqa: PLC0415
    from bridge.watchdog import Watchdog, health_check  # noqa: PLC0415

    setup_logging()
    settings = apply_installer_seed(load_settings(), seed_path())
    save_settings(settings)
    ctx = build_context(settings=settings)
    if port:
        ctx.settings.port = port
        ctx.save = lambda s, _port=port: save_settings(_with_port(s, _port))
    logger.info("%s %s iniciando en modo %s (datos en %s)", APP_NAME, __version__, mode, log_dir().parent)
    ctx.journal.purge(ctx.settings.retention_days)
    if demo:
        load_demo(ctx)

    monitor = PrinterMonitor(ctx.real, ctx.journal, lambda: ctx.settings, is_busy=ctx.service.is_busy)
    ctx.monitor = monitor
    monitor.start()
    ctx.updater = Updater(lambda: ctx.settings, ctx.journal, save=ctx.save)
    ctx.updater.record_startup()
    ctx.updater.start()
    worker = OutboxWorker(ctx.outbox, ctx.service, lambda: ctx.settings,
                          is_active=lambda: ctx.runner is not None and ctx.runner.state == "running")
    worker.start()
    runner = ServerRunner(ctx)
    runner.start()

    def on_hang():
        ctx.journal.record(kind="service", result="error", message="El puente no respondía: reinicio")
        if mode == "service":
            os._exit(1)  # Windows reinicia el servicio (acciones de recuperación)
        runner.restart()

    watchdog = Watchdog(health_check(lambda: ctx.settings.port, lambda: ctx.settings.api_key), on_hang)
    watchdog.start()

    stop = threading.Event()

    def purge_loop():
        while not stop.wait(6 * 3600):
            ctx.journal.purge(ctx.settings.retention_days)

    threading.Thread(target=purge_loop, daemon=True).start()
    return Core(ctx, [watchdog, worker, ctx.updater, monitor, runner], stop)


def run_tray_only() -> int:
    """Modo --tray: solo el ícono, hablando con el servicio por HTTP."""
    from bridge.config import local_token_path  # noqa: PLC0415
    from bridge.tray import HttpTrayBackend, run_tray  # noqa: PLC0415
    from bridge.tray_client import TrayClient  # noqa: PLC0415

    setup_logging()
    if single_instance("Local\\DimabePrintBridgeTray") is None:
        return 0
    settings = load_settings()
    try:
        token = local_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    stop = threading.Event()
    run_tray(HttpTrayBackend(TrayClient(settings.port, token)), on_quit=stop.set, quit_label="Cerrar ícono")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--service", action="store_true", help="correr como servicio de Windows (lo usa el instalador)")
    parser.add_argument("--core", action="store_true", help="núcleo sin bandeja, en primer plano")
    parser.add_argument("--tray", action="store_true", help="solo el ícono de bandeja (frente al servicio)")
    parser.add_argument("--no-tray", action="store_true", help="sin ícono de bandeja")
    parser.add_argument("--port", type=int, help="puerto local (sobrescribe la configuración solo en esta ejecución)")
    parser.add_argument("--demo", action="store_true", help="carga datos ficticios del día")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    if args.service:
        from bridge.winservice import run_service_dispatcher  # noqa: PLC0415
        run_service_dispatcher()
        return 0
    if args.tray:
        return run_tray_only()

    port = args.port or load_settings().port
    # Una instancia por puerto: dos en el mismo puerto no pueden convivir.
    if single_instance(f"Local\\DimabePrintBridge-{port}") is None:
        setup_logging()
        logger.info("Ya hay una instancia en el puerto %s; se sale.", port)
        return 0
    core = start_core(mode="core" if args.core else "all", port=args.port, demo=args.demo)
    from bridge.tray import LocalTrayBackend, run_tray  # noqa: PLC0415

    def print_test():
        from bridge.samples import test_ticket  # noqa: PLC0415
        usb = [p["label"] for p in core.ctx.monitor.snapshot() if p["kind"] == "usb"]
        if usb:
            core.ctx.service.print_job({"printer_name": usb[0], "raw_hex": test_ticket(usb[0]).hex(),
                                        "document_type": "test"}, None, "127.0.0.1", source="bandeja")

    if args.core or args.no_tray or not run_tray(LocalTrayBackend(core.ctx, print_test), on_quit=core.stop.set):
        try:
            while not core.stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
    core.shutdown()
    return 0


def _with_port(settings, _port):
    """Con --port no se guarda el puerto temporal en la configuración."""
    from bridge.config import load_settings as _load  # noqa: PLC0415
    saved = _load()
    copy = type(settings)(**vars(settings))
    copy.port = saved.port
    return copy


if __name__ == "__main__":
    raise SystemExit(main())
