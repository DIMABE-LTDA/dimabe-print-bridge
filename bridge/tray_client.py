"""Cliente HTTP que usa la bandeja para hablar con el servicio del puente."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class TrayClient:
    def __init__(self, port: int, token: str, timeout: float = 5):
        self.port = port
        self.token = token
        self.timeout = timeout
        self.base = f"http://127.0.0.1:{port}"

    def _request(self, path: str, body: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers.update({"Content-Type": "application/json", "X-CSRF-Token": self.token,
                            "Origin": self.base})
        request = urllib.request.Request(self.base + path, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - solo loopback
            return json.loads(response.read().decode("utf-8") or "{}")

    def status(self) -> dict | None:
        """Estado del panel o None si el servicio no responde."""
        try:
            return self._request("/panel/api/status")
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def set_test_mode(self, enabled: bool) -> dict:
        return self._request("/panel/api/settings", {"test_mode": bool(enabled)})

    def restart(self) -> dict:
        return self._request("/panel/api/service/restart", {})

    def print_test(self) -> dict:
        return self._request("/panel/api/service/test-ticket", {})
