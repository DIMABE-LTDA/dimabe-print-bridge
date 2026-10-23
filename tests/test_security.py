import pytest

from bridge.config import DEFAULT_ORIGINS
from bridge.errors import BridgeError
from bridge.security import (
    LoginThrottle,
    check_api_key,
    hash_password,
    ip_in_networks,
    is_origin_allowed,
    local_host_ok,
    validate_network_target,
    verify_password,
)

PORTS = list(range(9100, 9110))


@pytest.mark.parametrize(
    "origin",
    [
        "https://dimabe-odoo-grupo-los-lirios.odoo.com",
        "https://dimabe-odoo-grupo-los-lirios-staging-06072026-39556733.dev.odoo.com",
        "http://lirios.localhost",
        "https://www.gestion.grupoloslirios.cl",
        "http://localhost:8070",
        "http://127.0.0.1:3210",
        "HTTPS://Dimabe-Odoo-Grupo-Los-Lirios.odoo.com/",
        None,
        "",
    ],
)
def test_origins_allowed(origin):
    assert is_origin_allowed(origin, DEFAULT_ORIGINS)


@pytest.mark.parametrize("origin", ["https://evil.example.com", "https://odoo.com.evil.net", "null"])
def test_origins_rejected(origin):
    assert not is_origin_allowed(origin, DEFAULT_ORIGINS)


def test_star_allows_everything():
    assert is_origin_allowed("https://evil.example.com", ["*"])


@pytest.mark.parametrize(
    "ip,port,message",
    [
        ("8.8.8.8", 9100, "ip_address '8.8.8.8' is not a private network address."),
        ("google.com", 9100, "ip_address must be a literal IPv4 address."),
        ("127.0.0.1", 9100, "ip_address '127.0.0.1' is not a private network address."),
        ("192.168.1.10", 22, "ip_port 22 is not allowed. Allowed ports: 9100, 9101, 9102, 9103, 9104, 9105, 9106, 9107, 9108, 9109."),
    ],
)
def test_network_target_rejected(ip, port, message):
    with pytest.raises(BridgeError) as exc:
        validate_network_target(ip, port, PORTS)
    assert exc.value.status == 400
    assert exc.value.message == message


@pytest.mark.parametrize("ip", ["10.255.255.1", " 192.168.1.50 ", "172.16.0.9", "169.254.10.2"])
def test_network_target_allowed(ip):
    assert validate_network_target(ip, 9100, PORTS) == ip.strip()


def test_api_key():
    assert check_api_key(None, "")
    assert check_api_key("x", "")
    assert check_api_key("clave", "clave")
    assert not check_api_key("otra", "clave")
    assert not check_api_key(None, "clave")


def test_password_hash_roundtrip():
    stored = hash_password("Secreta#1")
    assert stored.startswith("pbkdf2_sha256$260000$")
    assert verify_password("Secreta#1", stored)
    assert not verify_password("secreta#1", stored)
    assert not verify_password("x", "")
    assert not verify_password("x", "basura")


def test_throttle_locks_after_five_and_releases():
    now = [1000.0]
    t = LoginThrottle(max_attempts=5, lock_seconds=600, clock=lambda: now[0])
    for _ in range(4):
        t.fail("1.2.3.4")
    assert not t.is_locked("1.2.3.4")
    t.fail("1.2.3.4")
    assert t.is_locked("1.2.3.4")
    assert not t.is_locked("5.6.7.8")
    now[0] += 601
    assert not t.is_locked("1.2.3.4")


def test_throttle_reset():
    t = LoginThrottle(max_attempts=2, lock_seconds=600)
    t.fail("a")
    t.reset("a")
    t.fail("a")
    assert not t.is_locked("a")


def test_ip_in_networks():
    assert ip_in_networks("192.168.1.40", ["192.168.1.0/24"])
    assert not ip_in_networks("192.168.2.40", ["192.168.1.0/24"])
    assert not ip_in_networks("192.168.1.40", [])
    assert ip_in_networks("127.0.0.1", [])
    assert not ip_in_networks("no-ip", ["192.168.1.0/24"])


def test_local_host_ok():
    assert local_host_ok("127.0.0.1:3210", 3210)
    assert local_host_ok("localhost:3210", 3210)
    assert not local_host_ok("evil.com:3210", 3210)
    assert not local_host_ok("127.0.0.1:3299", 3210)
    assert not local_host_ok(None, 3210)
