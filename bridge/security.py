"""Reglas de seguridad del puente: quién puede pedir qué y hacia dónde."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from typing import Callable

from bridge.errors import BridgeError

PBKDF2_ITERATIONS = 260000


def is_origin_allowed(origin: str | None, patterns: list[str]) -> bool:
    # Sin Origin no es una página web (curl, Invoke-RestMethod): se permite.
    if not origin or not origin.strip():
        return True
    normalized = origin.strip().rstrip("/").lower()
    return any(
        pattern == "*" or fnmatch.fnmatchcase(normalized, pattern.strip().rstrip("/").lower())
        for pattern in patterns
    )


def validate_network_target(ip: str, port: int, allowed_ports: list[int]) -> str:
    """Solo una IPv4 privada escrita tal cual y un puerto de impresora."""
    candidate = (ip or "").strip()
    try:
        address = ipaddress.IPv4Address(candidate)
    except ValueError as exc:
        raise BridgeError("ip_address must be a literal IPv4 address.", 400) from exc
    if address.is_loopback or not (address.is_private or address.is_link_local):
        raise BridgeError(f"ip_address '{candidate}' is not a private network address.", 400)
    if port not in allowed_ports:
        allowed = ", ".join(str(p) for p in allowed_ports)
        raise BridgeError(f"ip_port {port} is not allowed. Allowed ports: {allowed}.", 400)
    return candidate


def check_api_key(provided: str | None, expected: str) -> bool:
    if not expected:
        return True
    return hmac.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


class LoginThrottle:
    """Bloquea una IP por un rato tras varios intentos fallidos de entrar."""

    def __init__(self, max_attempts: int = 5, lock_seconds: int = 600, clock: Callable[[], float] = time.monotonic):
        self.max_attempts = max_attempts
        self.lock_seconds = lock_seconds
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_locked(self, ip: str) -> bool:
        with self._lock:
            until = self._locked_until.get(ip)
            if until is None:
                return False
            if self.clock() >= until:
                del self._locked_until[ip]
                self._failures.pop(ip, None)
                return False
            return True

    def fail(self, ip: str) -> None:
        with self._lock:
            count = self._failures.get(ip, 0) + 1
            self._failures[ip] = count
            if count >= self.max_attempts:
                self._locked_until[ip] = self.clock() + self.lock_seconds

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)


def ip_in_networks(ip: str, cidrs: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    for cidr in cidrs:
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def local_host_ok(host_header: str | None, port: int) -> bool:
    """Defensa contra DNS rebinding: el panel local solo responde a su propio Host."""
    if not host_header:
        return False
    return host_header.strip().lower() in (f"127.0.0.1:{port}", f"localhost:{port}")
