"""Páginas HTML del panel de TI."""
from __future__ import annotations

import socket

from flask import Blueprint, Response, current_app, redirect, render_template, request, session

from bridge import APP_NAME, __version__
from bridge.panel_api import csrf_token_for_page
from bridge.security import ip_in_networks, local_host_ok

PAGES = {
    "estado": ("/panel", "Estado"),
    "impresoras": ("/panel/impresoras", "Impresoras"),
    "bitacora": ("/panel/bitacora", "Bitácora"),
    "prueba": ("/panel/prueba", "Modo prueba"),
    "configuracion": ("/panel/configuracion", "Configuración"),
}


def _ctx():
    return current_app.config["BRIDGE_CTX"]


def _remote() -> bool:
    return current_app.config.get("BRIDGE_REMOTE", False)


def create_panel_views_blueprint() -> Blueprint:
    bp = Blueprint("panel_views", __name__)

    @bp.before_request
    def guard():
        ctx = _ctx()
        if request.endpoint == "panel_views.root":
            return None
        if _remote():
            if not ip_in_networks(request.remote_addr or "", ctx.settings.remote_networks):
                return Response("Este equipo no está en las redes permitidas para el panel.", 403)
            if request.endpoint != "panel_views.login" and not session.get("user"):
                return redirect("/panel/login")
            return None
        if not local_host_ok(request.host, ctx.settings.port):
            return Response("Host no permitido para el panel local.", 403)
        return None

    def render(page: str):
        ctx = _ctx()
        return render_template(
            f"{page}.html",
            page=page,
            pages=PAGES,
            title=PAGES[page][1],
            app_name=APP_NAME,
            version=__version__,
            csrf=csrf_token_for_page(),
            settings=ctx.settings,
            remote=_remote(),
            user=session.get("user") if _remote() else None,
            host=socket.gethostname(),
        )

    @bp.get("/")
    def root():
        return redirect("/panel")

    @bp.get("/panel")
    def estado():
        return render("estado")

    @bp.get("/panel/impresoras")
    def impresoras():
        return render("impresoras")

    @bp.get("/panel/bitacora")
    def bitacora():
        return render("bitacora")

    @bp.get("/panel/prueba")
    def prueba():
        return render("prueba")

    @bp.get("/panel/configuracion")
    def configuracion():
        return render("configuracion")

    @bp.get("/panel/login")
    def login():
        if not _remote():
            return redirect("/panel")
        return render_template("login.html", app_name=APP_NAME, version=__version__,
                               host=socket.gethostname(), error=bool(request.args.get("error")))

    return bp
