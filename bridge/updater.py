"""Actualización del puente desde el último Release de GitHub.

Solo instala un instalador firmado por el certificado de huella fija (la que
viene empaquetada en el build) y, si GitHub informa el SHA-256 del archivo,
que coincida. En modo `auto` instala de madrugada y sin impresiones recientes;
un script programado comprueba 5 minutos después que la versión nueva
responde y, si no, reinstala la anterior.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from bridge import __version__
from bridge.config import data_dir

logger = logging.getLogger(__name__)

DEFAULT_REPO = "DIMABE-LTDA/dimabe-print-bridge"
WINDOW_START, WINDOW_END = 3, 5  # 03:00–05:00
QUIET_MINUTES = 30
CHECK_EVERY = timedelta(hours=6)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def compare_versions(left: str, right: str) -> int:
    a = [int(p) if p.isdigit() else 0 for p in str(left).split(".")]
    b = [int(p) if p.isdigit() else 0 for p in str(right).split(".")]
    size = max(len(a), len(b))
    a += [0] * (size - len(a))
    b += [0] * (size - len(b))
    return (a > b) - (a < b)


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def pinned_thumbprint() -> str:
    try:
        return resource_path("installer/cert-thumbprint.txt").read_text(encoding="utf-8").strip().upper()
    except OSError:
        return ""


def fetch_latest_release(repo: str) -> dict | None:
    import requests  # noqa: PLC0415 - waitress/flask no lo traen; viene con el build
    try:
        response = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                                headers={"Accept": "application/vnd.github+json", "User-Agent": "dimabe-print-bridge"},
                                timeout=15)
    except requests.RequestException as exc:
        logger.warning("No se pudo consultar el último Release: %s", exc)
        return None
    return response.json() if response.status_code == 200 else None


def download_file(url: str, path: Path) -> None:
    import requests  # noqa: PLC0415
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "dimabe-print-bridge"}) as response:
        response.raise_for_status()
        with open(path, "wb") as handle:
            for chunk in response.iter_content(1 << 16):
                handle.write(chunk)


def verify_signature(path: Path) -> tuple[str | None, str]:
    """Huella del firmante y estado Authenticode, con PowerShell."""
    # La ruta va por variable de entorno: con -Command, PowerShell no la entrega en $args.
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:DIMABE_VERIFY_PATH; "
              "@{thumb=$s.SignerCertificate.Thumbprint; status=[string]$s.Status} | ConvertTo-Json -Compress")
    # Si el proceso nació de PowerShell 7, su PSModulePath impide a powershell.exe (5.1)
    # cargar Microsoft.PowerShell.Security. En Windows la clave puede venir en mayúsculas.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["DIMABE_VERIFY_PATH"] = str(path)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             text=True, timeout=60, env=env, creationflags=NO_WINDOW).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "error"
    return (data.get("thumb") or None), data.get("status") or "unknown"


def run_installer(path: Path, version: str) -> None:
    """Programa la verificación posterior y lanza el instalador en silencio."""
    previous = path.parent / "previous.exe"
    check = resource_path("installer/rollback-check.ps1")
    when = (datetime.now() + timedelta(minutes=5)).strftime("%H:%M")
    command = (f'powershell -NoProfile -ExecutionPolicy Bypass -File "{check}" -Expected {version} '
               f'-Previous "{previous}"')
    subprocess.run(["schtasks", "/Create", "/F", "/TN", "DimabePrintBridgeRollbackCheck", "/SC", "ONCE", "/ST", when,
                    "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", command], capture_output=True, timeout=30,
                   creationflags=NO_WINDOW)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"],
                     creationflags=flags, close_fds=True)


class Updater:
    def __init__(self, settings_provider: Callable, journal, repo: str = DEFAULT_REPO, current: str = __version__,
                 pinned: str | None = None, updates_dir: Path | None = None,
                 fetch=fetch_latest_release, download=download_file, verify=verify_signature,
                 run_installer=run_installer, clock: Callable[[], datetime] = datetime.now):
        self.settings_provider = settings_provider
        self.journal = journal
        self.repo = repo
        self.current = current
        self.pinned = (pinned if pinned is not None else pinned_thumbprint()).upper()
        self.updates_dir = Path(updates_dir) if updates_dir else data_dir() / "updates"
        self.fetch, self.download, self.verify, self.run_installer = fetch, download, verify, run_installer
        self.clock = clock
        self._release: dict | None = None
        self._state = {"available": False, "latest": None, "current": current, "checked_at": None, "last_error": None}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _mode(self) -> str:
        return getattr(self.settings_provider(), "update_mode", "auto")

    def state(self) -> dict:
        with self._lock:
            return {**self._state, "mode": self._mode()}

    def _asset(self) -> dict | None:
        for asset in (self._release or {}).get("assets") or []:
            name = asset.get("name") or ""
            if name.startswith("DimabePrintBridge-Setup") and name.endswith(".exe"):
                return asset
        return None

    def check(self) -> dict:
        if self._mode() == "off":
            return self.state()
        release = self.fetch(self.repo)
        with self._lock:
            self._state["checked_at"] = self.clock().isoformat(timespec="seconds")
            if release is None:
                self._state["last_error"] = "No se pudo consultar GitHub."
                return {**self._state, "mode": self._mode()}
            self._release = release
            latest = (release.get("tag_name") or "").lstrip("vV")
            self._state.update(latest=latest or None, last_error=None,
                               available=bool(latest and self._asset() and compare_versions(latest, self.current) > 0))
        return self.state()

    def _record(self, result: str, message: str) -> None:
        self.journal.record(kind="update", origin="actualizador", result=result, message=message)

    def maybe_install(self, now: datetime | None = None) -> str:
        now = now or self.clock()
        state = self.state()
        if not state["available"]:
            return "skipped:up_to_date"
        if state["mode"] != "auto":
            return "skipped:notify"
        if not WINDOW_START <= now.hour < WINDOW_END:
            return "skipped:outside_window"
        if self.journal.activity_since(QUIET_MINUTES):
            return "skipped:recent_activity"
        asset = self._asset()
        version = state["latest"]
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        target = self.updates_dir / asset["name"]
        try:
            self.download(asset["browser_download_url"], target)
        except Exception as exc:  # noqa: BLE001
            self._record("error", f"No se pudo descargar la versión {version}: {exc}")
            return "rejected:download"
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != digest.split(":", 1)[1].lower():
                target.unlink(missing_ok=True)
                self._record("error", f"Versión {version} rechazada: el archivo no coincide con el publicado.")
                return "rejected:hash"
        thumb, status = self.verify(target)
        if not self.pinned or (thumb or "").upper() != self.pinned:
            target.unlink(missing_ok=True)
            self._record("error", f"Versión {version} rechazada: firma de {thumb or 'nadie'} ({status}), "
                                  "no del certificado de Dimabe.")
            return "rejected:signature"
        current = self.updates_dir / "current.exe"
        if current.exists():
            shutil.copy2(current, self.updates_dir / "previous.exe")
        shutil.copy2(target, current)
        self._record("ok", f"Instalando la versión {version} (había {self.current}).")
        self.run_installer(target, version)
        return "installed"

    # -- hilo -------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_check = None
        while not self._stop.wait(300 if last_check else 60):
            try:
                if last_check is None or self.clock() - last_check >= CHECK_EVERY:
                    self.check()
                    last_check = self.clock()
                self.maybe_install()
            except Exception:  # noqa: BLE001
                logger.exception("Falló la revisión de actualizaciones")


__all__ = ["Updater", "compare_versions", "pinned_thumbprint", "os"]
