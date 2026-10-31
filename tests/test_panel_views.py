import re
from pathlib import Path

import pytest

from bridge.security import hash_password
from bridge.server import build_context, create_local_app, create_remote_app
from tests.test_service import FakeBackend

HOST = {"Host": "127.0.0.1:3210"}
PAGES = {
    "/panel": "Estado",
    "/panel/impresoras": "Impresoras",
    "/panel/bitacora": "Bitácora",
    "/panel/prueba": "Modo prueba",
    "/panel/configuracion": "Configuración",
}
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://|@import""", re.I)


@pytest.fixture
def ctx(data_dir):
    context = build_context(real=FakeBackend())
    yield context
    context.journal.stop()


@pytest.fixture
def local(ctx):
    return create_local_app(ctx).test_client()


@pytest.mark.parametrize("path,title", PAGES.items())
def test_pages_render(local, ctx, path, title):
    r = local.get(path, headers=HOST)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert f"<h2>{title}</h2>" in html
    assert f'<meta name="csrf-token" content="{ctx.csrf_token}">' in html
    assert not EXTERNAL.search(html)


def test_printers_page_explains_printer_on_another_machine(local):
    html = local.get("/panel/impresoras", headers=HOST).get_data(as_text=True)
    assert "La impresora está conectada a otro equipo" in html
    assert r"\\NOMBRE-DEL-EQUIPO\NOMBRE-COMPARTIDO" in html
    assert "Casos frecuentes" in html
    assert "Trabajo atascado en Windows" in html


def test_root_redirects_to_panel(local):
    r = local.get("/", headers=HOST)
    assert r.status_code == 302 and r.headers["Location"].endswith("/panel")


def test_local_page_rejects_foreign_host(local):
    assert local.get("/panel", headers={"Host": "evil.com:3210"}).status_code == 403


def test_static_assets_local_only():
    root = Path(__file__).resolve().parents[1] / "bridge" / "static"
    for name in ("panel.css", "panel.js"):
        assert not EXTERNAL.search((root / name).read_text(encoding="utf-8")), name


def test_test_mode_banner(local, ctx):
    ctx.settings.test_mode = True
    assert "Modo prueba encendido" in local.get("/panel", headers=HOST).get_data(as_text=True)


@pytest.fixture
def remote(ctx):
    ctx.settings.remote_panel_enabled = True
    ctx.settings.remote_user = "ti"
    ctx.settings.remote_password_hash = hash_password("Clave#2026")
    ctx.settings.remote_networks = ["192.168.1.0/24"]
    return create_remote_app(ctx).test_client()


RENV = {"REMOTE_ADDR": "192.168.1.40"}


def test_remote_redirects_to_login(remote):
    r = remote.get("/panel", environ_base=RENV)
    assert r.status_code == 302 and r.headers["Location"].endswith("/panel/login")
    login = remote.get("/panel/login", environ_base=RENV)
    assert login.status_code == 200 and "Entrar al panel" in login.get_data(as_text=True)


def test_remote_page_after_login(remote):
    remote.post("/panel/login", data={"user": "ti", "password": "Clave#2026"}, environ_base=RENV)
    r = remote.get("/panel/bitacora", environ_base=RENV)
    assert r.status_code == 200 and "<h2>Bitácora</h2>" in r.get_data(as_text=True)


def test_login_error_message(remote):
    r = remote.get("/panel/login?error=1", environ_base=RENV)
    assert "Usuario o clave incorrectos" in r.get_data(as_text=True)
