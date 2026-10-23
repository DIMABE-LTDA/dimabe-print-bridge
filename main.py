"""Punto de entrada de Dimabe Print Bridge."""
from __future__ import annotations

import argparse
import logging
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


def single_instance() -> object | None:
    """Mutex de Windows: una sola instancia por sesión."""
    try:
        import win32api  # noqa: PLC0415
        import win32event  # noqa: PLC0415
        import winerror  # noqa: PLC0415
    except ImportError:
        return object()
    mutex = win32event.CreateMutex(None, False, "Local\\DimabePrintBridge")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--no-tray", action="store_true", help="sin ícono de bandeja")
    parser.add_argument("--port", type=int, help="puerto local (sobrescribe la configuración solo en esta ejecución)")
    parser.add_argument("--demo", action="store_true", help="carga datos ficticios del día")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    setup_logging()
    mutex = single_instance()
    if mutex is None:
        logger.info("Ya hay una instancia corriendo; se sale.")
        return 0

    from bridge.monitor import PrinterMonitor  # noqa: PLC0415
    from bridge.server import ServerRunner, build_context  # noqa: PLC0415

    settings = apply_installer_seed(load_settings(), seed_path())
    save_settings(settings)
    ctx = build_context(settings=settings)
    if args.port:
        ctx.settings.port = args.port
        ctx.save = lambda s, _port=args.port: save_settings(_with_port(s, _port))
    logger.info("%s %s iniciando (datos en %s)", APP_NAME, __version__, log_dir().parent)
    ctx.journal.purge(ctx.settings.retention_days)
    if args.demo:
        load_demo(ctx)

    monitor = PrinterMonitor(ctx.real, ctx.journal, lambda: ctx.settings, is_busy=ctx.service.is_busy)
    ctx.monitor = monitor
    monitor.start()
    from bridge.updater import Updater  # noqa: PLC0415
    ctx.updater = Updater(lambda: ctx.settings, ctx.journal)
    ctx.updater.start()
    from bridge.outbox_worker import OutboxWorker  # noqa: PLC0415
    worker = OutboxWorker(ctx.outbox, ctx.service, lambda: ctx.settings)
    worker.start()
    runner = ServerRunner(ctx)
    runner.start()

    stop = threading.Event()

    def purge_loop():
        while not stop.wait(6 * 3600):
            ctx.journal.purge(ctx.settings.retention_days)

    threading.Thread(target=purge_loop, daemon=True).start()

    def shutdown():
        stop.set()
        worker.stop()
        ctx.updater.stop()
        monitor.stop()
        runner.stop()
        ctx.journal.record(kind="service", result="ok", message="Servicio detenido")
        ctx.journal.stop()

    def print_test():
        from bridge.samples import test_ticket  # noqa: PLC0415
        printers = [p for p in (monitor.snapshot() or []) if p["kind"] == "usb"]
        target = printers[0]["label"] if printers else None
        if not target:
            logger.warning("No hay impresora conocida para el ticket de prueba")
            return
        try:
            ctx.service.print_job({"printer_name": target, "raw_hex": test_ticket(target).hex(),
                                   "document_type": "test"}, None, "127.0.0.1", source="bandeja")
        except Exception:  # noqa: BLE001
            logger.exception("Falló el ticket de prueba")

    from bridge.tray import run_tray  # noqa: PLC0415
    if args.no_tray or not run_tray(ctx, on_quit=stop.set, on_print_test=print_test):
        try:
            while not stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
    shutdown()
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
