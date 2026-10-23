import base64
import json
from datetime import date

import pytest

from bridge.config import Settings, save_settings
from bridge.server import build_context, create_local_app
from tests.test_service import FakeBackend

ODOO = "https://dimabe-odoo-grupo-los-lirios.odoo.com"
EVIL = "https://evil.example.com"
HOST = {"Host": "127.0.0.1:3210"}


@pytest.fixture
def ctx(data_dir):
    context = build_context(real=FakeBackend())
    yield context
    context.journal.stop()


@pytest.fixture
def client(ctx):
    return create_local_app(ctx).test_client()


def post(client, path, payload, origin=ODOO, **headers):
    h = {"Origin": origin, "Content-Type": "application/json", **HOST, **headers}
    return client.post(path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=h)


@pytest.mark.parametrize("origin", [ODOO, "https://dimabe-odoo-grupo-los-lirios-staging-06072026-39556733.dev.odoo.com", "http://lirios.localhost"])
def test_preflight_allowed(client, origin):
    r = client.options("/api/print", headers={"Origin": origin, **HOST})
    assert r.status_code == 204
    assert r.data == b""
    assert r.headers["Access-Control-Allow-Origin"] == origin
    assert r.headers["Access-Control-Allow-Private-Network"] == "true"
    assert "X-Dimabe-Bridge-Key" in r.headers["Access-Control-Allow-Headers"]


def test_foreign_site_health_rejected(client, ctx):
    r = client.get("/api/health", headers={"Origin": EVIL, **HOST})
    assert r.status_code == 403
    assert r.get_json() == {"result": False, "status": "error", "message": "Origin not allowed by the bridge."}
    assert "Access-Control-Allow-Origin" not in r.headers
    ctx.journal.flush()
    assert ctx.journal.query_day(date.today())[0]["result"] == "rejected"


def test_foreign_site_cashbox_text_plain_rejected(client, ctx):
    r = client.post("/api/cashbox", data='{"printer_name":"X"}', headers={"Origin": EVIL, "Content-Type": "text/plain", **HOST})
    assert r.status_code == 403
    assert ctx.real.calls == []


def test_no_origin_health(client):
    r = client.get("/api/health", headers=HOST)
    body = r.get_json()
    assert r.status_code == 200
    assert body["result"] is True and body["version"] == "1.1.0" and body["test_mode"] is False
    assert body["message"] == "Dimabe Print Bridge is running."


@pytest.mark.parametrize("payload,fragment", [
    ({"ip_address": "8.8.8.8", "ip_port": 9100, "raw_hex": "1B40"}, "is not a private network address"),
    ({"ip_address": "google.com", "raw_hex": "1B40"}, "must be a literal IPv4 address"),
    ({"ip_address": "192.168.1.10", "ip_port": 22, "raw_hex": "1B40"}, "ip_port 22 is not allowed"),
])
def test_network_targets_rejected(client, ctx, payload, fragment):
    r = post(client, "/api/print", payload)
    assert r.status_code == 400
    body = r.get_json()
    assert body["result"] is False and fragment in body["message"]
    assert r.headers["Access-Control-Allow-Origin"] == ODOO
    assert ctx.real.calls == []


def test_private_ip_reaches_backend(client, ctx):
    r = post(client, "/api/print", {"ip_address": "10.255.255.1", "ip_port": 9100, "raw_hex": "1B40"})
    assert r.status_code == 200
    assert r.get_json()["message"] == "Raw bytes sent to '10.255.255.1:9100'."
    assert ctx.real.calls[0][1].label == "10.255.255.1:9100"


def test_printer_name_with_tildes(client, ctx):
    r = post(client, "/api/print", {"printer_name": "Impresora Cocina Ñandú", "raw_hex": "1B40"})
    assert r.status_code == 200
    assert ctx.real.calls[0][1].printer_name == "Impresora Cocina Ñandú"
    ctx.journal.flush()
    assert ctx.journal.query_day(date.today())[0]["target_label"] == "Impresora Cocina Ñandú"


def test_four_megabyte_receipt_prints(client, ctx):
    image = base64.b64encode(b"x" * 3_000_000).decode()
    r = post(client, "/api/print", {"printer_name": "B", "receipt": image})
    assert r.status_code == 200
    assert len(ctx.real.calls[0][2]) == 3_000_000


def test_oversized_body_is_413_json(client, ctx):
    big = "A" * (21 * 1024 * 1024)
    r = post(client, "/api/print", {"printer_name": "B", "raw_base64": big})
    assert r.status_code == 413
    assert r.get_json()["result"] is False
    assert ctx.real.calls == []


def test_invalid_json_is_400(client):
    r = client.post("/api/print", data="{no json", headers={"Origin": ODOO, "Content-Type": "application/json", **HOST})
    assert r.status_code == 400
    assert r.get_json()["result"] is False


def test_api_key(ctx):
    ctx.settings.api_key = "clave"
    client = create_local_app(ctx).test_client()
    assert client.get("/api/health", headers=HOST).status_code == 401
    r = client.get("/api/health", headers={"X-Dimabe-Bridge-Key": "clave", **HOST})
    assert r.status_code == 200
    assert r.get_json()["result"] is True
    assert client.get("/api/health", headers={"X-Dimabe-Bridge-Key": "otra", **HOST}).get_json()["message"] == "Invalid bridge API key."


def test_cashbox(client, ctx):
    r = post(client, "/api/cashbox", {"printer_name": "BIXOLON"})
    assert r.get_json()["message"] == "Cash drawer pulse sent to 'BIXOLON'."
    assert ctx.real.calls[0][0] == "cashbox"


def test_printers_list(client):
    r = client.get("/api/printers", headers=HOST)
    assert r.status_code == 200
    assert r.get_json()["printers"] == []


def test_unknown_route_404(client):
    r = client.get("/api/nada", headers=HOST)
    assert r.status_code == 404
    assert r.get_json()["message"] == "Route not found."


def test_test_mode_persisted_across_restart(data_dir):
    save_settings(Settings(test_mode=True))
    context = build_context(real=FakeBackend())
    try:
        client = create_local_app(context).test_client()
        assert client.get("/api/health", headers=HOST).get_json()["test_mode"] is True
        r = post(client, "/api/print", {"printer_name": "B", "raw_hex": "1B40"})
        assert r.get_json()["test_mode"] is True
        assert context.real.calls == []
        printers = client.get("/api/printers", headers=HOST).get_json()["printers"]
        assert printers[-1]["Type"] == "Virtual"
    finally:
        context.journal.stop()


def test_status_endpoint(client, ctx):
    r = client.get("/api/status", headers={"Origin": ODOO, **HOST})
    body = r.get_json()
    assert r.status_code == 200 and r.headers["Access-Control-Allow-Origin"] == ODOO
    assert body["result"] is True and body["version"] == "1.1.0" and body["test_mode"] is False
    assert body["queue"] == {"total": 0, "by_target": {}, "oldest_age_s": 0, "expired": 0}
    assert set(body["update"]) >= {"available", "latest", "mode"}
    assert isinstance(body["printers"], list) and isinstance(body["recent_errors"], dict)


def test_status_rejects_foreign_site(client):
    assert client.get("/api/status", headers={"Origin": EVIL, **HOST}).status_code == 403
