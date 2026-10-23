"""Rutas de datos y ajustes persistentes del puente.

Los datos viven en %PROGRAMDATA%\\DimabePrintBridge para que lo que se
ingresa al instalar (con la cuenta de administrador) lo vea también la
cuenta que usa la caja. DIMABE_BRIDGE_DATA_DIR lo sobreescribe en
desarrollo y en las pruebas.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ORIGINS = [
    "https://*.odoo.com",
    "http://*.localhost",
    "http://*.localhost:*",
    "http://localhost",
    "http://localhost:*",
    "http://127.0.0.1",
    "http://127.0.0.1:*",
]
TEST_RESPONSES = ("ok", "no_paper", "offline", "timeout", "one_in_three")
TEST_DELAYS = (0, 1, 5, 20)


def data_dir() -> Path:
    override = os.environ.get("DIMABE_BRIDGE_DATA_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("PROGRAMDATA") or os.path.expanduser("~")
        path = Path(base) / "DimabePrintBridge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return data_dir() / "settings.json"


def journal_path() -> Path:
    return data_dir() / "bitacora.sqlite"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed_path() -> Path:
    return data_dir() / "installer-seed.json"


@dataclass
class Settings:
    port: int = 3210
    api_key: str = ""
    allowed_origins: list[str] = field(default_factory=lambda: list(DEFAULT_ORIGINS))
    allowed_printer_ports: list[int] = field(default_factory=lambda: list(range(9100, 9110)))
    default_paper_width_mm: int = 80
    retention_days: int = 90
    store_payload: bool = True
    store_images: bool = False
    test_mode: bool = False
    test_response: str = "ok"
    test_delay_s: int = 0
    remote_panel_enabled: bool = False
    remote_panel_port: int = 3211
    remote_user: str = ""
    remote_password_hash: str = ""
    remote_networks: list[str] = field(default_factory=list)
    session_secret: str = ""
    installer_seed_applied: bool = False


def _valid_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _sanitize(s: Settings) -> Settings:
    default = Settings()
    if not _valid_port(s.port):
        s.port = default.port
    if not _valid_port(s.remote_panel_port):
        s.remote_panel_port = default.remote_panel_port
    if not isinstance(s.retention_days, int) or not 1 <= s.retention_days <= 3650:
        s.retention_days = default.retention_days
    if not isinstance(s.default_paper_width_mm, int) or s.default_paper_width_mm < 48:
        s.default_paper_width_mm = default.default_paper_width_mm
    if s.test_response not in TEST_RESPONSES:
        s.test_response = default.test_response
    if s.test_delay_s not in TEST_DELAYS:
        s.test_delay_s = default.test_delay_s
    if not isinstance(s.allowed_origins, list) or not all(isinstance(o, str) for o in s.allowed_origins):
        s.allowed_origins = default.allowed_origins
    if not isinstance(s.allowed_printer_ports, list) or not all(_valid_port(p) for p in s.allowed_printer_ports):
        s.allowed_printer_ports = default.allowed_printer_ports
    if not isinstance(s.remote_networks, list):
        s.remote_networks = []
    return s


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError) as exc:
        logger.warning("settings.json ilegible, se usan valores por defecto: %s", exc)
        return Settings()
    if not isinstance(raw, dict):
        return Settings()
    known = {f.name for f in fields(Settings)}
    return _sanitize(Settings(**{k: v for k, v in raw.items() if k in known}))


def save_settings(s: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(s), handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def apply_installer_seed(s: Settings, seed: Path) -> Settings:
    """Aplica una sola vez lo que el instalador dejó en installer-seed.json."""
    if s.installer_seed_applied or not seed.exists():
        return s
    try:
        values = json.loads(seed.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        logger.warning("installer-seed.json ilegible: %s", exc)
        values = {}
    if isinstance(values, dict):
        if _valid_port(values.get("port")):
            s.port = values["port"]
        if isinstance(values.get("api_key"), str):
            s.api_key = values["api_key"].strip()
        if isinstance(values.get("remote_panel_enabled"), bool):
            s.remote_panel_enabled = values["remote_panel_enabled"]
    s.installer_seed_applied = True
    try:
        seed.unlink()
    except OSError:
        pass
    return s
