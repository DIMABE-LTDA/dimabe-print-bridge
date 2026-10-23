"""Armado de la aplicación: contexto compartido y apps Flask local y remota."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable

from flask import Flask

from bridge.api import create_api_blueprint, register_api_errors
from bridge.backends import Backend
from bridge.capture import CaptureStore, VirtualBackend
from bridge.config import Settings, journal_path, load_settings, save_settings
from bridge.journal import Journal
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


def create_local_app(ctx: AppContext) -> Flask:
    app = _base_app(ctx)
    app.register_blueprint(create_api_blueprint())
    return app
