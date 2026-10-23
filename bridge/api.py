"""API compatible con el POS (misma forma que el puente PowerShell)."""
from __future__ import annotations

import json
import logging

from flask import Blueprint, Response, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from bridge import __version__
from bridge.errors import BridgeError
from bridge.security import check_api_key, is_origin_allowed

logger = logging.getLogger(__name__)


def _ctx():
    return current_app.config["BRIDGE_CTX"]


def _error(message: str, status: int, code: str | None = None):
    body = {"result": False, "status": "error", "message": message}
    if code:
        body["code"] = code
    response = jsonify(body)
    response.status_code = status
    return response


def _payload() -> dict:
    try:
        raw = request.get_data(cache=False)
    except RequestEntityTooLarge:
        raise
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BridgeError("Invalid JSON body.", 400) from exc
    if not isinstance(value, dict):
        raise BridgeError("JSON body must be an object.", 400)
    return value


def add_cors_headers(response: Response) -> Response:
    """CORS para las rutas /api: solo se devuelve Allow-Origin a sitios permitidos."""
    response.headers["Vary"] = "Origin"
    origin = request.headers.get("Origin")
    if origin and is_origin_allowed(origin, _ctx().settings.allowed_origins):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Dimabe-Bridge-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


def create_api_blueprint() -> Blueprint:
    bp = Blueprint("api", __name__, url_prefix="/api")

    @bp.before_request
    def guard():
        ctx = _ctx()
        origin = request.headers.get("Origin")
        if not is_origin_allowed(origin, ctx.settings.allowed_origins):
            logger.warning("Petición rechazada desde el origen %r", origin)
            ctx.journal.record(kind="rejected", origin=origin, client_ip=request.remote_addr,
                               result="rejected", http_status=403,
                               message="Origin not allowed by the bridge.", document_type=None,
                               test_mode=ctx.settings.test_mode)
            return _error("Origin not allowed by the bridge.", 403)
        if request.method == "OPTIONS":
            return Response(status=204)
        if not check_api_key(request.headers.get("X-Dimabe-Bridge-Key"), ctx.settings.api_key):
            return _error("Invalid bridge API key.", 401)
        return None

    @bp.after_request
    def cors(response):
        return add_cors_headers(response)

    @bp.route("/health", methods=["GET", "OPTIONS"])
    def health():
        ctx = _ctx()
        origin = request.headers.get("Origin") or ""
        if ctx.journal.should_log_health(origin):
            ctx.journal.record(kind="health", origin=origin or None, client_ip=request.remote_addr,
                               result="ok", http_status=200, message="Consulta de estado",
                               test_mode=ctx.settings.test_mode)
        return jsonify({
            "result": True,
            "status": "success",
            "message": "Dimabe Print Bridge is running.",
            "listen_url": f"http://127.0.0.1:{ctx.settings.port}/",
            "version": __version__,
            "test_mode": ctx.settings.test_mode,
        })

    @bp.route("/printers", methods=["GET", "OPTIONS"])
    def printers():
        ctx = _ctx()
        try:
            found = list(ctx.real.list_printers())
        except Exception as exc:  # el spooler puede fallar; el POS igual necesita respuesta
            logger.error("No se pudieron listar las impresoras: %s", exc)
            found = []
        if ctx.settings.test_mode:
            found += ctx.virtual.list_printers()
        return jsonify({"result": True, "status": "success", "printers": found})

    @bp.route("/print", methods=["POST", "OPTIONS"])
    def print_route():
        ctx = _ctx()
        return jsonify(ctx.service.print_job(_payload(), request.headers.get("Origin"), request.remote_addr))

    @bp.route("/cashbox", methods=["POST", "OPTIONS"])
    def cashbox():
        ctx = _ctx()
        return jsonify(ctx.service.cashbox(_payload(), request.headers.get("Origin"), request.remote_addr))

    @bp.errorhandler(BridgeError)
    def bridge_error(exc: BridgeError):
        return _error(exc.message, exc.status, exc.code)

    return bp


def register_api_errors(app) -> None:
    """Respuestas JSON para errores fuera de las vistas (413, 404, 500)."""

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        response = _error("El trabajo supera el máximo de 20 MB.", 413)
        return add_cors_headers(response) if request.path.startswith("/api") else response

    @app.errorhandler(404)
    def not_found(_exc):
        if request.path.startswith("/api") or request.path.startswith("/panel/api"):
            response = _error("Route not found.", 404)
            return add_cors_headers(response) if request.path.startswith("/api") else response
        return Response("No encontrado", status=404, mimetype="text/plain")

    @app.errorhandler(BridgeError)
    def bridge_error(exc: BridgeError):
        return _error(exc.message, exc.status, exc.code)

    @app.errorhandler(Exception)
    def unexpected(exc):
        if hasattr(exc, "code") and isinstance(getattr(exc, "code"), int):
            return _error(getattr(exc, "description", str(exc)), exc.code)
        logger.exception("Error inesperado")
        response = _error(str(exc), 500)
        return add_cors_headers(response) if request.path.startswith("/api") else response
