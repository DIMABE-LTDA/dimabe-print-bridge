"""Armado de la aplicación: contexto compartido y apps Flask local y remota."""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from dataclasses import dataclass, field
from typing import Callable

from flask import Flask

from bridge.api import create_api_blueprint, register_api_errors
from bridge.backends import Backend
from bridge.capture import CaptureStore, VirtualBackend
from bridge.config import Settings, journal_path, load_settings, local_token_path, outbox_path, save_settings
from bridge.journal import Journal
from bridge.outbox import Outbox
from bridge.panel_api import create_auth_blueprint, create_panel_api_blueprint
from bridge.service import PrintService

MAX_BODY_BYTES = 20 * 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    save: Callable[[Settings], None]
    journal: Journal
    service: PrintService
    captures: CaptureStore
    real: Backend
    virtual: VirtualBackend
    outbox: Outbox | None = None
    updater: object | None = None
    monitor: object | None = None
    runner: object | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))


def ensure_local_token() -> str:
    """Token del panel local, compartido por el servicio y la bandeja (archivo en ProgramData)."""
    path = local_token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
        path.write_text(token, encoding="utf-8")
    return token


def build_context(real: Backend | None = None, settings: Settings | None = None) -> AppContext:
    settings = settings or load_settings()
    if real is None:
        from bridge.backends import WindowsBackend  # noqa: PLC0415
        real = WindowsBackend()
    journal = Journal(journal_path())
    journal.start()
    captures = CaptureStore()
    holder: dict = {}
    provider = lambda: holder["ctx"].settings  # noqa: E731  (sigue al objeto vigente)
    virtual = VirtualBackend(captures, provider)
    outbox = Outbox(outbox_path())
    service = PrintService(provider, real, virtual, journal, outbox=outbox)
    ctx = AppContext(settings=settings, save=save_settings, journal=journal, service=service,
                     captures=captures, real=real, virtual=virtual, outbox=outbox, csrf_token=ensure_local_token())
    holder["ctx"] = ctx
    return ctx


def _base_app(ctx: AppContext) -> Flask:
    app = Flask(__name__)
    app.config["BRIDGE_CTX"] = ctx
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False
    register_api_errors(app)
    return app


def _register_panel(app: Flask) -> None:
    app.register_blueprint(create_auth_blueprint())
    app.register_blueprint(create_panel_api_blueprint())
    try:
        from bridge.panel_views import create_panel_views_blueprint  # noqa: PLC0415
    except ImportError:
        return
    app.register_blueprint(create_panel_views_blueprint())


def create_local_app(ctx: AppContext) -> Flask:
    app = _base_app(ctx)
    app.config["BRIDGE_REMOTE"] = False
    app.register_blueprint(create_api_blueprint())
    _register_panel(app)
    return app


def create_remote_app(ctx: AppContext) -> Flask:
    """Solo el panel, con login. No expone /api/print ni /api/cashbox."""
    if not ctx.settings.session_secret:
        ctx.settings.session_secret = secrets.token_hex(32)
        ctx.save(ctx.settings)
    app = _base_app(ctx)
    app.config.update(
        BRIDGE_REMOTE=True,
        SECRET_KEY=ctx.settings.session_secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="dimabe_bridge_panel",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    _register_panel(app)
    return app


class ServerRunner:
    """Levanta waitress para la app local y, si corresponde, la remota.

    Si el puerto está ocupado (por ejemplo, por el puente PowerShell viejo)
    no se cae: queda en «port_busy» y reintenta cada `retry_seconds`.
    """

    def __init__(self, ctx: AppContext, retry_seconds: float = 10, threads: int = 8):
        import threading  # noqa: PLC0415
        self.ctx = ctx
        self.retry_seconds = retry_seconds
        self.threads = threads
        self.state = "stopped"
        self.busy_by = ""
        self.since: str | None = None
        self._servers: list = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.on_state_change: Callable[[str], None] | None = None
        ctx.runner = self

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if self.on_state_change:
                try:
                    self.on_state_change(state)
                except Exception:  # noqa: BLE001
                    logger.exception("on_state_change falló")

    def start(self) -> None:
        import threading  # noqa: PLC0415
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="server-runner", daemon=True)
        self._thread.start()

    def _create(self, app, host: str, port: int):
        from waitress.server import create_server  # noqa: PLC0415
        return create_server(app, host=host, port=port, threads=self.threads, ident="DimabePrintBridge",
                             clear_untrusted_proxy_headers=True, channel_timeout=30)

    def _run(self) -> None:
        import threading  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415
        while not self._stop.is_set():
            settings = self.ctx.settings
            try:
                local = self._create(create_local_app(self.ctx), "127.0.0.1", settings.port)
            except OSError as exc:
                was_busy = self.state == "port_busy"
                self._set_state("port_busy")
                if not was_busy:
                    self.busy_by = port_owner(settings.port)
                    logger.warning("Puerto %s ocupado (%s): %s", settings.port, self.busy_by or "?", exc)
                self._stop.wait(self.retry_seconds)
                continue
            servers = [local]
            if settings.remote_panel_enabled:
                try:
                    servers.append(self._create(create_remote_app(self.ctx), "0.0.0.0", settings.remote_panel_port))
                except OSError as exc:
                    logger.error("No se pudo abrir el panel remoto en %s: %s", settings.remote_panel_port, exc)
            with self._lock:
                self._servers = servers
            self.busy_by = ""
            self.since = datetime.now().astimezone().isoformat(timespec="seconds")
            self._set_state("running")
            self.ctx.journal.record(kind="service", result="ok",
                                    message=f"Servicio en línea en 127.0.0.1:{settings.port}",
                                    test_mode=settings.test_mode)
            workers = [threading.Thread(target=s.run, daemon=True) for s in servers[1:]]
            for w in workers:
                w.start()
            try:
                local.run()
            except Exception:  # noqa: BLE001
                logger.exception("El servidor local se detuvo con error")
            for s in servers[1:]:
                s.close()
            if not self._stop.is_set():
                self._stop.wait(0.5)
        self._set_state("stopped")

    def _close_servers(self) -> None:
        with self._lock:
            servers, self._servers = self._servers, []
        for server in servers:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
        self._close_servers()
        if self._thread:
            self._thread.join(timeout=5)

    def restart(self) -> None:
        """Cierra los servidores; el bucle los vuelve a crear con la configuración vigente."""
        self.ctx.journal.record(kind="service", result="ok", message="Reinicio del servicio")
        self._close_servers()

    def restart_later(self, delay: float = 1.0) -> None:
        import threading  # noqa: PLC0415
        threading.Timer(delay, self.restart).start()


def port_owner(port: int) -> str:
    """Nombre y PID del proceso que escucha en el puerto (mejor esfuerzo)."""
    import subprocess  # noqa: PLC0415
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=10, creationflags=flags).stdout
        pid = next((line.split()[-1] for line in out.splitlines()
                    if f":{port} " in line and "LISTEN" in line.upper()), "")
        if not pid:
            return ""
        task = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                              text=True, timeout=10, creationflags=flags).stdout.strip()
        name = task.split(",")[0].strip('"') if task else "?"
        return f"{name} (PID {pid})"
    except (OSError, subprocess.SubprocessError, StopIteration):
        return ""
