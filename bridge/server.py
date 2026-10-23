"""Armado de la aplicación: contexto compartido y apps Flask local y remota."""
from __future__ import annotations

import secrets
from datetime import timedelta
from dataclasses import dataclass, field
from typing import Callable

from flask import Flask

from bridge.api import create_api_blueprint, register_api_errors
from bridge.backends import Backend
from bridge.capture import CaptureStore, VirtualBackend
from bridge.config import Settings, journal_path, load_settings, save_settings
from bridge.journal import Journal
from bridge.panel_api import create_auth_blueprint, create_panel_api_blueprint
from bridge.service import PrintService

MAX_BODY_BYTES = 20 * 1024 * 1024


@dataclass
class AppContext:
    settings: Settings
    save: Callable[[Settings], None]
    journal: Journal
    service: PrintService
    captures: CaptureStore
    real: Backend
    virtual: VirtualBackend
    monitor: object | None = None
    runner: object | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))


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
    service = PrintService(provider, real, virtual, journal)
    ctx = AppContext(settings=settings, save=save_settings, journal=journal, service=service,
                     captures=captures, real=real, virtual=virtual)
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
