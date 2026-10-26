"""API JSON del panel de TI y su autenticación (local y desde la red)."""
from __future__ import annotations

import base64
import logging
import secrets
import socket
import subprocess
from datetime import date

from flask import Blueprint, Response, current_app, jsonify, redirect, request, session

from bridge import __version__
from bridge.backends import Target
from bridge.config import TEST_DELAYS, TEST_RESPONSES, UPDATE_MODES
from bridge.errors import BridgeError
from bridge.escpos import looks_like_zpl, render_escpos
from bridge.samples import SAMPLES, test_ticket
from bridge.security import (
    LoginThrottle,
    hash_password,
    ip_in_networks,
    local_host_ok,
    verify_password,
)

logger = logging.getLogger(__name__)

EDITABLE_BOOLS = ("store_payload", "store_images", "test_mode", "remote_panel_enabled")
RESTART_KEYS = ("port", "remote_panel_enabled", "remote_panel_port")


def _ctx():
    return current_app.config["BRIDGE_CTX"]


def _is_remote() -> bool:
    return current_app.config.get("BRIDGE_REMOTE", False)


def _json_error(message: str, status: int):
    response = jsonify({"result": False, "status": "error", "message": message})
    response.status_code = status
    return response


def default_firewall(port: int) -> str:
    """Abre el puerto del panel remoto en el firewall de Windows (requiere admin)."""
    command = ["netsh", "advfirewall", "firewall", "add", "rule", "name=Dimabe Print Bridge panel",
               "dir=in", "action=allow", "protocol=TCP", f"localport={port}"]
    try:
        result = subprocess.run(command, capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "ok" if result.returncode == 0 else "needs_admin"
    except (OSError, subprocess.SubprocessError):
        return "needs_admin"


def firewall_command(port: int) -> str:
    return (f'netsh advfirewall firewall add rule name="Dimabe Print Bridge panel" dir=in '
            f"action=allow protocol=TCP localport={port}")


# -- autenticación ---------------------------------------------------------
def check_access(require_csrf: bool):
    """Devuelve una respuesta de error o None si la petición puede seguir."""
    ctx = _ctx()
    if _is_remote():
        if not ip_in_networks(request.remote_addr or "", ctx.settings.remote_networks):
            return _json_error("Este equipo no está en las redes permitidas para el panel.", 403)
        if not session.get("user"):
            return _json_error("Inicia sesión para usar el panel.", 401)
        if require_csrf and request.headers.get("X-CSRF-Token") != session.get("csrf"):
            return _json_error("Falta el token de seguridad del panel. Recarga la página.", 403)
        return None
    if not local_host_ok(request.host, ctx.settings.port):
        return _json_error("Host no permitido para el panel local.", 403)
    if require_csrf:
        origin = request.headers.get("Origin")
        allowed = (f"http://127.0.0.1:{ctx.settings.port}", f"http://localhost:{ctx.settings.port}")
        if origin and origin.rstrip("/") not in allowed:
            return _json_error("Origen no permitido para acciones del panel.", 403)
        if request.headers.get("X-CSRF-Token") != ctx.csrf_token:
            return _json_error("Falta el token de seguridad del panel. Recarga la página.", 403)
    return None


def csrf_token_for_page() -> str:
    if _is_remote():
        if not session.get("csrf"):
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]
    return _ctx().csrf_token


def create_auth_blueprint() -> Blueprint:
    bp = Blueprint("panel_auth", __name__, url_prefix="/panel")
    throttle = LoginThrottle()

    @bp.post("/login")
    def login():
        ctx = _ctx()
        ip = request.remote_addr or ""
        if not _is_remote():
            return redirect("/panel")
        if not ip_in_networks(ip, ctx.settings.remote_networks):
            return _json_error("Este equipo no está en las redes permitidas para el panel.", 403)
        if throttle.is_locked(ip):
            return _json_error("Demasiados intentos. El acceso quedó bloqueado por 10 minutos.", 429)
        user = (request.form.get("user") or "").strip()
        password = request.form.get("password") or ""
        if user == ctx.settings.remote_user and verify_password(password, ctx.settings.remote_password_hash):
            throttle.reset(ip)
            session.clear()
            session.permanent = True
            session["user"] = user
            session["csrf"] = secrets.token_urlsafe(32)
            ctx.journal.record(kind="config", origin="panel-remoto", client_ip=ip, result="ok",
                               message=f"Ingreso al panel desde la red ({user})")
            return redirect("/panel")
        throttle.fail(ip)
        ctx.journal.record(kind="rejected", origin="panel-remoto", client_ip=ip, result="rejected",
                           http_status=401, message="Clave incorrecta en el panel remoto")
        if throttle.is_locked(ip):
            return _json_error("Demasiados intentos. El acceso quedó bloqueado por 10 minutos.", 429)
        return redirect("/panel/login?error=1")

    @bp.post("/logout")
    def logout():
        session.clear()
        return redirect("/panel/login")

    return bp


# -- utilidades -----------------------------------------------------------
def _parse_day(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BridgeError("Fecha inválida: usa el formato AAAA-MM-DD.", 400) from exc


def _target_from(body: dict) -> Target:
    target = body.get("target") or {}
    if not isinstance(target, dict):
        raise BridgeError("Destino inválido.", 400)
    return _ctx().service.parse_target(target)


def _target_payload(target: Target) -> dict:
    if target.kind == "ip":
        return {"ip_address": target.ip, "ip_port": target.port}
    return {"printer_name": target.printer_name}


def _preview(kind: str, data: bytes) -> dict:
    if kind == "image":
        return {"kind": "image", "png_base64": base64.b64encode(data).decode("ascii")}
    if kind == "cashbox":
        return {"kind": "cashbox"}
    if looks_like_zpl(data):
        return {"kind": "zpl", "zpl_text": data.decode("utf-8", errors="replace")}
    return {"kind": "escpos", "lines": render_escpos(data)}


def _printers_snapshot(ctx, summary: dict) -> list[dict]:
    if ctx.monitor is not None:
        return ctx.monitor.snapshot()
    return [
        {"label": label, "kind": "ip" if ":" in label else "usb",
         "status": "error" if info["error"] and not info["ok"] else "ok",
         "status_text": "Con errores hoy" if info["error"] else "Lista",
         "today_ok": info["ok"], "today_error": info["error"],
         "last_ts": info["last_ts"], "last_message": info["last_message"]}
        for label, info in sorted(summary["by_target"].items())
    ]


def _attention(ctx, printers: list[dict]) -> list[dict]:
    items = []
    for label, count in sorted(ctx.journal.recent_errors(30).items()):
        items.append({"level": "error", "title": f"{label} tiene errores",
                      "detail": f"{count} trabajo(s) fallaron en los últimos 30 minutos. "
                                "Revisa que esté encendida, con papel y conectada.",
                      "link": f"/panel/bitacora?target={label}"})
    for printer in printers:
        if printer.get("status") == "warn":
            items.append({"level": "warn", "title": f"{printer['label']}: {printer['status_text']}",
                          "detail": "Los trabajos quedan en la cola de Windows sin salir.",
                          "link": "/panel/impresoras"})
    if ctx.outbox is not None:
        counts = ctx.outbox.counts()
        if counts["total"]:
            minutes = round(counts["oldest_age_s"] / 60)
            items.append({"level": "warn", "title": f"{counts['total']} comanda(s) en cola",
                          "detail": f"Esperan que vuelva su impresora (la más antigua hace {minutes} min). "
                                    "Se imprimen solas al volver.", "link": "/panel#cola"})
        expired = ctx.outbox.expired()
        if expired:
            items.append({"level": "error", "title": f"{len(expired)} comanda(s) vencidas sin imprimir",
                          "detail": "Pasaron 2 horas en cola sin que volviera la impresora. Avisa a cocina.",
                          "link": "/panel#cola"})
    s = ctx.settings
    updater = getattr(ctx, "updater", None)
    if updater is not None and updater.state()["available"]:
        state = updater.state()
        when = "esta madrugada" if state["mode"] == "auto" else "cuando TI la instale"
        items.append({"level": "warn", "title": f"Versión nueva disponible ({state['latest']})",
                      "detail": f"Tienes la {state['current']}. Se instala {when}.", "link": "/panel/configuracion#actualizacion"})
    if s.test_mode:
        items.append({"level": "test", "title": "Modo prueba encendido",
                      "detail": "Nada se imprime en papel ni abre el cajón.", "link": "/panel/prueba"})
    # La clave API es opcional: sin ella el puente igual solo acepta los sitios permitidos.
    if s.remote_panel_enabled and not s.remote_password_hash:
        items.append({"level": "warn", "title": "Panel abierto a la red sin clave",
                      "detail": "Define usuario y clave del panel.", "link": "/panel/configuracion#acceso"})
    return items


def _public_settings(s) -> dict:
    return {
        "port": s.port, "api_key_set": bool(s.api_key), "allowed_origins": s.allowed_origins,
        "allowed_printer_ports": s.allowed_printer_ports, "default_paper_width_mm": s.default_paper_width_mm,
        "retention_days": s.retention_days, "store_payload": s.store_payload, "store_images": s.store_images,
        "test_mode": s.test_mode, "test_response": s.test_response, "test_delay_s": s.test_delay_s,
        "remote_panel_enabled": s.remote_panel_enabled, "remote_panel_port": s.remote_panel_port,
        "remote_user": s.remote_user, "remote_password_set": bool(s.remote_password_hash),
        "remote_networks": s.remote_networks, "update_mode": s.update_mode,
    }


def _parse_ports(value) -> list[int]:
    if isinstance(value, list):
        ports = value
    elif isinstance(value, str):
        ports = []
        for part in value.replace(" ", "").split(","):
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                ports.extend(range(int(start), int(end) + 1))
            else:
                ports.append(int(part))
    else:
        raise ValueError
    ports = sorted({int(p) for p in ports})
    if not ports or any(not 1 <= p <= 65535 for p in ports):
        raise ValueError
    return ports


def _apply_settings(s, body: dict) -> list[str]:
    """Valida y aplica; devuelve las claves cambiadas o lanza BridgeError 400."""
    changed = []

    def set_(key, value):
        if getattr(s, key) != value:
            setattr(s, key, value)
            changed.append(key)

    for key in ("port", "remote_panel_port"):
        if key in body:
            value = body[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                raise BridgeError("El puerto debe ser un número entre 1 y 65535.", 400)
            set_(key, value)
    if "retention_days" in body:
        value = body["retention_days"]
        if not isinstance(value, int) or not 1 <= value <= 3650:
            raise BridgeError("Los días de bitácora deben estar entre 1 y 3650.", 400)
        set_("retention_days", value)
    if "default_paper_width_mm" in body:
        if body["default_paper_width_mm"] not in (58, 76, 80):
            raise BridgeError("El ancho de papel debe ser 58, 76 u 80 mm.", 400)
        set_("default_paper_width_mm", body["default_paper_width_mm"])
    if "allowed_printer_ports" in body:
        try:
            set_("allowed_printer_ports", _parse_ports(body["allowed_printer_ports"]))
        except (ValueError, TypeError) as exc:
            raise BridgeError("Puertos inválidos: usa por ejemplo 9100-9109 o 9100,9101.", 400) from exc
    if "allowed_origins" in body:
        origins = body["allowed_origins"]
        if not isinstance(origins, list) or not all(isinstance(o, str) and o.strip() for o in origins):
            raise BridgeError("Lista de sitios inválida.", 400)
        set_("allowed_origins", [o.strip() for o in origins])
    if "remote_networks" in body:
        import ipaddress  # noqa: PLC0415
        nets = body["remote_networks"]
        try:
            if not isinstance(nets, list):
                raise ValueError
            for net in nets:
                ipaddress.ip_network(net, strict=False)
        except ValueError as exc:
            raise BridgeError("Redes inválidas: usa el formato 192.168.1.0/24.", 400) from exc
        set_("remote_networks", nets)
    if "test_response" in body:
        if body["test_response"] not in TEST_RESPONSES:
            raise BridgeError("Respuesta simulada desconocida.", 400)
        set_("test_response", body["test_response"])
    if "update_mode" in body:
        if body["update_mode"] not in UPDATE_MODES:
            raise BridgeError("Modo de actualización inválido.", 400)
        set_("update_mode", body["update_mode"])
    if "test_delay_s" in body:
        if body["test_delay_s"] not in TEST_DELAYS:
            raise BridgeError("Demora simulada inválida.", 400)
        set_("test_delay_s", body["test_delay_s"])
    for key in EDITABLE_BOOLS:
        if key in body:
            if not isinstance(body[key], bool):
                raise BridgeError(f"Valor inválido para {key}.", 400)
            set_(key, body[key])
    if s.remote_panel_enabled and not (s.remote_user and s.remote_password_hash):
        raise BridgeError("Define usuario y clave antes de abrir el panel a la red.", 400)
    return changed


# -- blueprint --------------------------------------------------------------
def create_panel_api_blueprint() -> Blueprint:
    bp = Blueprint("panel_api", __name__, url_prefix="/panel/api")

    @bp.before_request
    def guard():
        return check_access(require_csrf=request.method == "POST")

    @bp.errorhandler(BridgeError)
    def bridge_error(exc: BridgeError):
        return _json_error(exc.message, exc.status)

    @bp.get("/status")
    def status():
        ctx = _ctx()
        summary = ctx.journal.summary_day(date.today())
        printers = _printers_snapshot(ctx, summary)
        runner = ctx.runner
        return jsonify({
            "service": {
                "version": __version__,
                "listen_url": f"http://127.0.0.1:{ctx.settings.port}/",
                "since": getattr(runner, "since", None),
                "state": getattr(runner, "state", "running"),
                "api_key_set": bool(ctx.settings.api_key),
                "remote_enabled": ctx.settings.remote_panel_enabled,
                "test_mode": ctx.settings.test_mode,
                "host": socket.gethostname(),
            },
            "today": summary,
            "printers": printers,
            "attention": _attention(ctx, printers),
            "last_rejected": ctx.journal.last_rejected(),
            "journal": ctx.journal.size_info(),
        })

    @bp.get("/printers")
    def printers():
        ctx = _ctx()
        summary = ctx.journal.summary_day(date.today())
        try:
            windows = ctx.real.list_printers()
        except Exception as exc:  # noqa: BLE001
            logger.error("No se pudieron listar las impresoras: %s", exc)
            windows = []
        return jsonify({"printers": _printers_snapshot(ctx, summary), "windows": windows,
                        "virtual": ctx.virtual.list_printers() if ctx.settings.test_mode else []})

    @bp.post("/printers/test")
    def printer_test():
        ctx = _ctx()
        body = request.get_json(silent=True) or {}
        target = _target_from(body)
        kind = body.get("kind")
        if kind == "connection":
            if target.kind == "ip":
                try:
                    with socket.create_connection((target.ip, target.port), timeout=2):
                        pass
                    return jsonify({"result": True, "message": f"{target.label} responde."})
                except OSError as exc:
                    return jsonify({"result": False, "message": f"{target.label} no responde: {exc}"})
            state = ctx.real.queue_status(target.printer_name)
            return jsonify({"result": state == "ok", "message": f"Estado en Windows: {state}"})
        if kind == "cashbox":
            return jsonify(ctx.service.cashbox(_target_payload(target), None, request.remote_addr, source="panel"))
        if kind == "label":
            data = SAMPLES["etiqueta"][1]()
            payload = {**_target_payload(target), "raw_hex": data.hex(), "document_type": "label"}
        elif kind == "text":
            payload = {**_target_payload(target), "raw_hex": test_ticket(target.label).hex(), "document_type": "test"}
        else:
            raise BridgeError("Tipo de prueba desconocido.", 400)
        return jsonify(ctx.service.print_job(payload, None, request.remote_addr, source="panel"))

    @bp.get("/journal")
    def journal():
        ctx = _ctx()
        day = _parse_day(request.args.get("day"))
        rows = ctx.journal.query_day(
            day, result=request.args.get("result") or None, target=request.args.get("target") or None,
            document_type=request.args.get("document_type") or None, text=request.args.get("q") or None,
            limit=min(int(request.args.get("limit", 200)), 500), offset=int(request.args.get("offset", 0)))
        return jsonify({"day": day.isoformat(), "rows": rows, "summary": ctx.journal.summary_day(day)})

    @bp.get("/journal/<int:event_id>")
    def journal_detail(event_id: int):
        event = _ctx().journal.get(event_id)
        if not event:
            raise BridgeError("Registro no encontrado.", 404)
        if event.get("payload"):
            preview = _preview(event["payload_kind"], bytes.fromhex(event["payload"]))
            event["preview_kind"] = preview["kind"]
            event["preview"] = preview.get("lines") or preview.get("zpl_text") or preview.get("png_base64")
        return jsonify(event)

    @bp.get("/journal/<day>.csv")
    def journal_csv(day: str):
        parsed = _parse_day(day)
        return Response(_ctx().journal.export_csv(parsed), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="bitacora-{parsed.isoformat()}.csv"'})

    @bp.post("/journal/<int:event_id>/reprint")
    def journal_reprint(event_id: int):
        body = request.get_json(silent=True) or {}
        target = _target_from(body) if body.get("target") else None
        return jsonify(_ctx().service.reprint(event_id, target))

    @bp.post("/journal/purge-all")
    def journal_purge():
        ctx = _ctx()
        ctx.journal.delete_all()
        ctx.journal.record(kind="config", origin="panel", result="ok", message="Bitácora borrada desde el panel")
        return jsonify({"result": True, "message": "Bitácora borrada."})

    @bp.get("/test/jobs")
    def test_jobs():
        ctx = _ctx()
        jobs = ctx.captures.list(request.args.get("target") or None)
        return jsonify({
            "jobs": [{"id": j.id, "ts": j.ts, "target_label": j.target_label, "document_type": j.document_type,
                      "kind": j.kind, "bytes": len(j.data)} for j in jobs],
            "targets": ctx.captures.targets(),
        })

    @bp.get("/test/jobs/<int:job_id>/preview")
    def test_preview(job_id: int):
        job = _ctx().captures.get(job_id)
        if not job:
            raise BridgeError("Trabajo no encontrado.", 404)
        return jsonify({**_preview(job.kind, job.data), "hex": job.data[:4096].hex()})

    @bp.post("/test/clear")
    def test_clear():
        _ctx().captures.clear()
        return jsonify({"result": True})

    @bp.post("/test/sample")
    def test_sample():
        ctx = _ctx()
        body = request.get_json(silent=True) or {}
        if body.get("sample") not in SAMPLES:
            raise BridgeError("Ejemplo desconocido.", 400)
        if not ctx.settings.test_mode and not body.get("confirm"):
            raise BridgeError("El modo prueba está apagado: esto imprimiría en papel. Confirma para seguir.", 409)
        document_type, factory = SAMPLES[body["sample"]]
        payload = {**_target_payload(_target_from(body)), "raw_hex": factory().hex(), "document_type": document_type}
        return jsonify(ctx.service.print_job(payload, None, request.remote_addr, source="panel"))

    @bp.post("/test/forward")
    def test_forward():
        ctx = _ctx()
        body = request.get_json(silent=True) or {}
        job = ctx.captures.get(int(body.get("job_id") or 0))
        if not job:
            raise BridgeError("Trabajo no encontrado.", 404)
        target = _target_from(body)
        backend = ctx.real
        job_name = f"Dimabe {job.document_type}"
        if job.kind == "image":
            backend.print_image(target, job.data, ctx.settings.default_paper_width_mm, job_name)
        elif job.kind == "cashbox":
            backend.open_cashbox(target)
        else:
            backend.send_raw(target, job.data, job_name)
        ctx.journal.record(kind="print", document_type=job.document_type, target_label=target.label,
                           target_kind=target.kind, origin="panel", client_ip=request.remote_addr,
                           bytes=len(job.data), result="ok", http_status=200,
                           message=f"Trabajo de prueba reenviado a {target.label}")
        return jsonify({"result": True, "message": f"Enviado a {target.label}."})

    @bp.post("/service/restart")
    def service_restart():
        ctx = _ctx()
        if ctx.runner is None:
            raise BridgeError("El servicio no se puede reiniciar desde aquí.", 409)
        ctx.runner.restart_later()
        return jsonify({"result": True, "message": "Reiniciando el servicio."})

    @bp.post("/service/test-ticket")
    def service_test_ticket():
        """Ticket de prueba en la primera impresora USB conocida (lo usa la bandeja)."""
        ctx = _ctx()
        printers = ctx.monitor.snapshot() if ctx.monitor is not None else []
        usb = [p["label"] for p in printers if p["kind"] == "usb"]
        if not usb:
            try:
                usb = [p["Name"] for p in ctx.real.list_printers()]
            except Exception:  # noqa: BLE001
                usb = []
        if not usb:
            raise BridgeError("No hay una impresora conocida para el ticket de prueba.", 404)
        payload = {"printer_name": usb[0], "raw_hex": test_ticket(usb[0]).hex(), "document_type": "test"}
        return jsonify(ctx.service.print_job(payload, None, request.remote_addr, source="bandeja"))

    @bp.get("/queue")
    def queue_list():
        ctx = _ctx()
        if ctx.outbox is None:
            return jsonify({"pending": [], "counts": {"total": 0, "by_target": {}, "oldest_age_s": 0}, "expired": []})
        return jsonify({"pending": ctx.outbox.pending(), "counts": ctx.outbox.counts(),
                        "expired": ctx.outbox.expired()})

    @bp.post("/queue/<int:queue_id>/retry")
    def queue_retry(queue_id: int):
        if _ctx().outbox is None:
            raise BridgeError("Este puente no tiene cola.", 404)
        _ctx().outbox.retry_now(queue_id)
        return jsonify({"result": True, "message": "Se reintenta en unos segundos."})

    @bp.post("/queue/<int:queue_id>/discard")
    def queue_discard(queue_id: int):
        ctx = _ctx()
        if ctx.outbox is None:
            raise BridgeError("Este puente no tiene cola.", 404)
        ctx.outbox.discard(queue_id)
        ctx.journal.record(kind="queue", origin="panel", result="ok", message=f"Trabajo {queue_id} descartado de la cola")
        return jsonify({"result": True, "message": "Trabajo descartado."})

    @bp.get("/settings")
    def settings_get():
        return jsonify(_public_settings(_ctx().settings))

    @bp.post("/settings")
    def settings_post():
        ctx = _ctx()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise BridgeError("Cuerpo inválido.", 400)
        candidate = type(ctx.settings)(**vars(ctx.settings))
        changed = _apply_settings(candidate, body)
        was_remote = ctx.settings.remote_panel_enabled
        ctx.settings.__dict__.update(vars(candidate))
        ctx.save(ctx.settings)
        if changed:
            ctx.journal.record(kind="config", origin="panel", client_ip=request.remote_addr, result="ok",
                               message="Configuración cambiada: " + ", ".join(changed),
                               test_mode=ctx.settings.test_mode)
        response = {"result": True, "settings": _public_settings(ctx.settings),
                    "restart_required": any(k in changed for k in RESTART_KEYS)}
        if ctx.settings.remote_panel_enabled and (not was_remote or "remote_panel_port" in changed):
            firewall = current_app.config.get("FIREWALL", default_firewall)
            response["firewall"] = firewall(ctx.settings.remote_panel_port)
            if response["firewall"] != "ok":
                response["firewall_command"] = firewall_command(ctx.settings.remote_panel_port)
        if response["restart_required"] and ctx.runner is not None:
            ctx.runner.restart_later()
        return jsonify(response)

    @bp.post("/settings/api-key/generate")
    def generate_key():
        ctx = _ctx()
        ctx.settings.api_key = secrets.token_urlsafe(24)
        ctx.save(ctx.settings)
        ctx.journal.record(kind="config", origin="panel", result="ok", message="Clave API regenerada")
        return jsonify({"result": True, "api_key": ctx.settings.api_key})

    @bp.post("/settings/remote-password")
    def remote_password():
        ctx = _ctx()
        body = request.get_json(silent=True) or {}
        user = (body.get("user") or "").strip()
        password = body.get("password") or ""
        if not user or len(password) < 8:
            raise BridgeError("Usuario obligatorio y clave de al menos 8 caracteres.", 400)
        ctx.settings.remote_user = user
        ctx.settings.remote_password_hash = hash_password(password)
        ctx.save(ctx.settings)
        ctx.journal.record(kind="config", origin="panel", result="ok", message="Clave del panel remoto cambiada")
        return jsonify({"result": True})

    return bp
