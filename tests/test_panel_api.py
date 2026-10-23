from datetime import date

import pytest

from bridge.security import hash_password
from bridge.server import build_context, create_local_app, create_remote_app
from tests.test_service import FakeBackend

HOST = {"Host": "127.0.0.1:3210"}


@pytest.fixture
def ctx(data_dir):
    context = build_context(real=FakeBackend())
    yield context
    context.journal.stop()


@pytest.fixture
def local(ctx):
    app = create_local_app(ctx)
    app.config["FIREWALL"] = lambda port: "ok"
    return app.test_client()


def lpost(client, ctx, path, json=None, **headers):
    return client.post(path, json=json or {}, headers={**HOST, "X-CSRF-Token": ctx.csrf_token,
                                                      "Origin": "http://127.0.0.1:3210", **headers})


# -- autenticación local ------------------------------------------------
def test_local_rejects_foreign_host(local):
    assert local.get("/panel/api/status", headers={"Host": "evil.com:3210"}).status_code == 403


def test_local_get_ok_without_login(local):
    r = local.get("/panel/api/status", headers=HOST)
    assert r.status_code == 200
    assert r.get_json()["service"]["version"] == "1.1.0"


def test_local_post_requires_csrf(local, ctx):
    assert local.post("/panel/api/test/clear", headers=HOST).status_code == 403
    assert local.post("/panel/api/test/clear", headers={**HOST, "X-CSRF-Token": "x"}).status_code == 403
    assert lpost(local, ctx, "/panel/api/test/clear").status_code == 200


def test_local_post_rejects_foreign_origin(local, ctx):
    r = lpost(local, ctx, "/panel/api/test/clear", Origin="https://evil.example.com")
    assert r.status_code == 403


# -- estado y atención ---------------------------------------------------
def test_status_attention(local, ctx):
    ctx.settings.test_mode = True
    lpost(local, ctx, "/panel/api/test/sample", {"sample": "comanda", "target": {"printer_name": "Cocina"}})
    ctx.journal.flush()
    body = local.get("/panel/api/status", headers=HOST).get_json()
    titles = [a["title"] for a in body["attention"]]
    assert "Modo prueba encendido" in titles
    assert "Sin clave API" in titles
    assert body["today"]["ok"] == 1
    assert body["service"]["test_mode"] is True


def test_status_reports_recent_errors(local, ctx):
    ctx.real.fail = "No hubo conexión"
    local.post("/api/print", json={"ip_address": "192.168.1.50", "raw_hex": "1B40"}, headers=HOST)
    ctx.journal.flush()
    body = local.get("/panel/api/status", headers=HOST).get_json()
    assert any("192.168.1.50:9100" in a["title"] for a in body["attention"])


# -- bitácora ---------------------------------------------------------------
def test_journal_endpoints(local, ctx):
    local.post("/api/print", json={"printer_name": "B", "raw_hex": "1B4041"}, headers=HOST)
    ctx.journal.flush()
    today = date.today().isoformat()
    rows = local.get(f"/panel/api/journal?day={today}", headers=HOST).get_json()["rows"]
    assert len(rows) == 1
    detail = local.get(f"/panel/api/journal/{rows[0]['id']}", headers=HOST).get_json()
    assert detail["payload"] == "1b4041"
    assert detail["preview"][0]["text"] == "A"
    csv = local.get(f"/panel/api/journal/{today}.csv", headers=HOST)
    assert csv.headers["Content-Disposition"].endswith(f'bitacora-{today}.csv"')
    r = lpost(local, ctx, f"/panel/api/journal/{rows[0]['id']}/reprint")
    assert r.get_json()["result"] is True
    assert len(ctx.real.calls) == 2


def test_journal_bad_day_is_400(local):
    assert local.get("/panel/api/journal?day=ayer", headers=HOST).status_code == 400


# -- modo prueba ------------------------------------------------------------
def test_samples_and_preview(local, ctx):
    ctx.settings.test_mode = True
    for sample in ("boleta", "comanda", "etiqueta"):
        r = lpost(local, ctx, "/panel/api/test/sample", {"sample": sample, "target": {"printer_name": "Virtual"}})
        assert r.get_json()["result"] is True
    jobs = local.get("/panel/api/test/jobs", headers=HOST).get_json()["jobs"]
    assert [j["document_type"] for j in jobs] == ["label", "kitchen", "receipt"]
    preview = local.get(f"/panel/api/test/jobs/{jobs[1]['id']}/preview", headers=HOST).get_json()
    assert preview["kind"] == "escpos" and any(l.get("text") == "MESA 12" for l in preview["lines"])
    zpl = local.get(f"/panel/api/test/jobs/{jobs[0]['id']}/preview", headers=HOST).get_json()
    assert zpl["kind"] == "zpl" and "^XA" in zpl["zpl_text"]
    assert ctx.real.calls == []


def test_sample_refused_outside_test_mode_without_confirm(local, ctx):
    r = lpost(local, ctx, "/panel/api/test/sample", {"sample": "comanda", "target": {"printer_name": "B"}})
    assert r.status_code == 409


def test_forward_to_real_printer(local, ctx):
    ctx.settings.test_mode = True
    lpost(local, ctx, "/panel/api/test/sample", {"sample": "comanda", "target": {"printer_name": "V"}})
    job_id = local.get("/panel/api/test/jobs", headers=HOST).get_json()["jobs"][0]["id"]
    r = lpost(local, ctx, "/panel/api/test/forward", {"job_id": job_id, "target": {"printer_name": "Real"}})
    assert r.get_json()["result"] is True
    assert ctx.real.calls[0][1].printer_name == "Real"


def test_clear_jobs(local, ctx):
    ctx.captures.add("x", "receipt", "raw", b"1")
    lpost(local, ctx, "/panel/api/test/clear")
    assert local.get("/panel/api/test/jobs", headers=HOST).get_json()["jobs"] == []


# -- configuración --------------------------------------------------------
def test_settings_hide_secrets(local, ctx):
    ctx.settings.api_key = "secreta"
    ctx.settings.remote_password_hash = hash_password("x")
    body = local.get("/panel/api/settings", headers=HOST).get_json()
    assert body["api_key_set"] is True
    assert "api_key" not in body and "remote_password_hash" not in body and "session_secret" not in body


def test_settings_update_and_validation(local, ctx):
    r = lpost(local, ctx, "/panel/api/settings", {"retention_days": 30, "allowed_printer_ports": "9100-9102", "test_mode": True})
    assert r.status_code == 200
    assert (ctx.settings.retention_days, ctx.settings.allowed_printer_ports, ctx.settings.test_mode) == (30, [9100, 9101, 9102], True)
    from bridge.config import load_settings
    assert load_settings().retention_days == 30
    assert lpost(local, ctx, "/panel/api/settings", {"retention_days": 0}).status_code == 400
    assert lpost(local, ctx, "/panel/api/settings", {"port": 70000}).status_code == 400
    assert lpost(local, ctx, "/panel/api/settings", {"port": 3300}).get_json()["restart_required"] is True


def test_remote_needs_password_then_opens_firewall(local, ctx):
    assert lpost(local, ctx, "/panel/api/settings", {"remote_panel_enabled": True}).status_code == 400
    lpost(local, ctx, "/panel/api/settings/remote-password", {"user": "ti", "password": "Clave#2026"})
    r = lpost(local, ctx, "/panel/api/settings", {"remote_panel_enabled": True, "remote_networks": ["192.168.1.0/24"]})
    assert r.get_json()["firewall"] == "ok"


def test_generate_api_key(local, ctx):
    key = lpost(local, ctx, "/panel/api/settings/api-key/generate").get_json()["api_key"]
    assert len(key) >= 24 and ctx.settings.api_key == key


# -- panel remoto -----------------------------------------------------------
@pytest.fixture
def remote(ctx):
    ctx.settings.remote_panel_enabled = True
    ctx.settings.remote_user = "ti"
    ctx.settings.remote_password_hash = hash_password("Clave#2026")
    ctx.settings.remote_networks = ["192.168.1.0/24"]
    return create_remote_app(ctx).test_client()


RENV = {"REMOTE_ADDR": "192.168.1.40"}


def test_remote_requires_login(remote):
    assert remote.get("/panel/api/status", environ_base=RENV).status_code == 401


def test_remote_login_and_status(remote):
    r = remote.post("/panel/login", data={"user": "ti", "password": "Clave#2026"}, environ_base=RENV)
    assert r.status_code in (200, 302)
    assert remote.get("/panel/api/status", environ_base=RENV).status_code == 200


def test_remote_lockout(remote):
    for _ in range(5):
        remote.post("/panel/login", data={"user": "ti", "password": "mala"}, environ_base=RENV)
    r = remote.post("/panel/login", data={"user": "ti", "password": "Clave#2026"}, environ_base=RENV)
    assert r.status_code == 429


def test_remote_outside_network(remote):
    r = remote.post("/panel/login", data={"user": "ti", "password": "Clave#2026"}, environ_base={"REMOTE_ADDR": "10.0.0.5"})
    assert r.status_code == 403


def test_remote_has_no_print_api(remote):
    remote.post("/panel/login", data={"user": "ti", "password": "Clave#2026"}, environ_base=RENV)
    assert remote.post("/api/print", json={"printer_name": "B", "raw_hex": "1B40"}, environ_base=RENV).status_code == 404
    assert remote.post("/api/cashbox", json={"printer_name": "B"}, environ_base=RENV).status_code == 404
