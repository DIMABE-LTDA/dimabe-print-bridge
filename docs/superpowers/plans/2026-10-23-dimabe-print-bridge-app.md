# Dimabe Print Bridge (app) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reemplazar el puente PowerShell por una app de Windows (bandeja + panel web para TI + bitácora SQLite + modo prueba + instalador firmado) compatible con el POS actual, y permitir descargar el instalador desde Odoo.

**Architecture:** Paquete Python `bridge/` con lógica pura y testeable (seguridad, bitácora, intérprete ESC/POS, servicio de impresión) separada de los adaptadores (Flask/waitress, `win32print`, `pystray`). Dos apps Flask: la local en `127.0.0.1:3210` (API compatible + panel) y la remota opcional en `0.0.0.0:3211` (solo panel con login). Odoo agrega un modelo de versiones del instalador y un controlador de descarga en `dimabe_pos`.

**Tech Stack:** Python 3.12, Flask 3, waitress 3, pywin32, Pillow, pystray, sqlite3 (stdlib), pytest; PyInstaller; Inno Setup 6; PowerShell `Set-AuthenticodeSignature`. Odoo 19 (`dimabe_pos`).

**Spec:** `docs/superpowers/specs/2026-10-23-dimabe-print-bridge-app-design.md`

## Global Constraints

- Repo: `C:\Users\fabia\Documents\Desarrollo\Trabajo\proyectos\dimabe_print_bridge`. Venv en `.venv` (Python 3.12). Commits con `-c user.name=fabiandroguettdimabe -c user.email=fabian.droguett@dimabe.cl` y la línea `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Nombre visible: «Dimabe Print Bridge». Versión inicial `1.0.0` en `bridge/__init__.py` (`__version__`), única fuente.
- Datos: `%PROGRAMDATA%\DimabePrintBridge` (`settings.json`, `bitacora.sqlite`, `logs\bridge.log` rotativo 5×2 MB); variable `DIMABE_BRIDGE_DATA_DIR` la sobreescribe (tests y desarrollo).
- API local: `127.0.0.1:3210`. Panel remoto: `0.0.0.0:3211`, apagado por defecto.
- Orígenes por defecto, exactos: `https://*.odoo.com`, `http://*.localhost`, `http://*.localhost:*`, `http://localhost`, `http://localhost:*`, `http://127.0.0.1`, `http://127.0.0.1:*`. Petición sin `Origin` = permitida.
- Impresoras de red: IPv4 literal privada (10/8, 172.16/12, 192.168/16, 169.254/16; nunca loopback), puertos `9100..9109` por defecto.
- Pulso de cajón: bytes `1B 70 00 19 FA`.
- Header API key: `X-Dimabe-Bridge-Key`. Respuestas `{result, status, message, ...}`; códigos 200/204/400/401/403/404/413/500. Body máximo 20 MB.
- Timeouts red: conexión 3 s, escritura 5 s. Dedupe `job_id`: 300 s.
- Retención bitácora: 1–3650 días, por defecto 90. Purga al iniciar y cada 6 h.
- UI en español de Chile; fuente de verdad visual `maquetas/panel-completo.html`; sin CDN en runtime (IBM Plex Sans/Mono vendorizadas como woff2 en `bridge/static/fonts/`).
- Odoo: rama `feat/print-bridge-descarga-odoo` desde `origin/PRD` en worktree `proyectos/_worktrees/print-bridge-descarga-23102026`; `dimabe_pos` sube de `19.0.7.33.0` a `19.0.7.35.0` (la 7.34.0 la usa la rama del fix). Solo `_inherit` sobre el core; vistas `<list>`.

## Review Focus

1. **Recibo de varios MB y payload > 20 MB:** el de 4 MB imprime; el de 21 MB responde 413 JSON con `result:false` sin colgar el servidor → test en Task 7.
2. **Nombre de impresora con tildes/ñ** («Impresora Cocina Ñandú») llega intacto hasta el backend y la bitácora → test en Task 7.
3. **Dos trabajos simultáneos a la misma impresora** no se intercalan (lock por destino) y a impresoras distintas sí corren en paralelo → test en Task 6.
4. **Falla de SQLite (disco lleno, base bloqueada)** nunca hace fallar una impresión: el trabajo se imprime y el error queda en el log de texto → test en Task 3.
5. **Modo prueba encendido sobrevive a un reinicio** y siempre se anuncia (`test_mode:true` en `/api/health` y en cada respuesta) → test en Task 7.

---

## File Structure

```
bridge/__init__.py          versión
bridge/config.py            rutas de datos, Settings (dataclass) y persistencia
bridge/errors.py            BridgeError(message, status)
bridge/security.py          orígenes, destinos de red, API key, contraseñas, throttle, CIDR
bridge/journal.py           bitácora SQLite (cola + hilo escritor, consultas, purga, CSV)
bridge/escpos.py            intérprete ESC/POS → líneas para la vista previa; detección ZPL
bridge/backends.py          WindowsBackend (RAW spooler, imagen, TCP, estado de cola) y VirtualBackend
bridge/capture.py           CaptureStore + simulación del modo prueba
bridge/service.py           PrintService: destino, dedupe, locks, modo prueba, bitácora
bridge/api.py               blueprint /api/* compatible + CORS/origen/API key/413
bridge/panel_api.py         blueprint /panel/api/* (JSON) + auth local/remota + CSRF
bridge/panel_views.py       blueprint /panel (HTML) y /panel/login
bridge/templates/           base.html, estado.html, impresoras.html, bitacora.html, prueba.html, configuracion.html, login.html
bridge/static/              panel.css, panel.js, icons.svg, fonts/*.woff2, samples/ (boleta.bin, comanda.bin, etiqueta.zpl)
bridge/monitor.py           refresco de estado de impresoras cada 60 s
bridge/server.py            AppContext, create_local_app, create_remote_app, ServerRunner (waitress, reintento de puerto)
bridge/tray.py              ícono de bandeja con colores y menú
main.py                     punto de entrada
tests/                      un test_*.py por módulo
DimabePrintBridge.spec      PyInstaller
installer/                  DimabePrintBridge.iss, build.ps1, crear-certificado.ps1, instalar-certificado.ps1, dimabe-print-bridge.cer
README.md, BUILD.md, MANUAL_TI.md, requirements.txt, requirements-dev.txt, pytest.ini
```

---

### Task 1: Esqueleto, configuración y errores

**Files:**
- Create: `bridge/__init__.py`, `bridge/config.py`, `bridge/errors.py`, `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `bridge.__version__ = "1.0.0"`
  - `class BridgeError(Exception)`: `__init__(self, message: str, status: int = 500)`; atributos `message`, `status`.
  - `data_dir() -> Path` (crea la carpeta), `settings_path() -> Path`, `journal_path() -> Path`, `log_dir() -> Path`.
  - `@dataclass Settings` con campos y defaults: `port=3210`, `api_key=""`, `allowed_origins=<lista global>`, `allowed_printer_ports=list(range(9100,9110))`, `default_paper_width_mm=80`, `retention_days=90`, `store_payload=True`, `store_images=False`, `test_mode=False`, `test_response="ok"` (`ok|no_paper|offline|timeout|one_in_three`), `test_delay_s=0` (`0|1|5|20`), `remote_panel_enabled=False`, `remote_panel_port=3211`, `remote_user=""`, `remote_password_hash=""`, `remote_networks: list[str]=[]`, `installer_seed_applied=False`.
  - `load_settings(path: Path | None = None) -> Settings` (archivo ausente o JSON roto → defaults; claves desconocidas se ignoran; valores fuera de rango se corrigen a default), `save_settings(s: Settings, path: Path | None = None) -> None` (escritura atómica: temp + replace).
  - `apply_installer_seed(s: Settings, seed_path: Path) -> Settings`: si existe `installer-seed.json` (claves `port`, `api_key`, `remote_panel_enabled`) y `installer_seed_applied` es False, lo aplica, marca True y borra el seed.
  - `tests/conftest.py`: fixture `data_dir` que apunta `DIMABE_BRIDGE_DATA_DIR` a `tmp_path`.

- [ ] **Step 1:** Crear `requirements.txt` (`flask>=3.0`, `waitress>=3.0`, `pywin32>=306; sys_platform=="win32"`, `Pillow>=10.0`, `pystray>=0.19`) y `requirements-dev.txt` (`-r requirements.txt`, `pytest>=8`, `pyinstaller>=6`); crear `.venv` con `py -3.12 -m venv .venv` e instalar dev.
- [ ] **Step 2: Tests que fallan** en `tests/test_config.py`: `test_defaults_match_spec` (port 3210, retention 90, ports 9100..9109, los 7 orígenes exactos), `test_roundtrip_utf8` (api_key `"ñandú"` persiste), `test_corrupt_json_returns_defaults`, `test_out_of_range_retention_resets` (`retention_days=0` y `99999` → 90), `test_seed_applied_once` (seed con port 3300 y api_key → aplicados, seed borrado, segunda llamada no cambia nada).
- [ ] **Step 3:** `.venv\Scripts\python -m pytest tests/test_config.py -v` → FAIL (módulo inexistente).
- [ ] **Step 4:** Implementar `bridge/config.py`, `bridge/errors.py`, `bridge/__init__.py`.
- [ ] **Step 5:** Mismo comando → PASS.
- [ ] **Step 6:** Commit `feat: configuración persistente y errores base`.

### Task 2: Seguridad

**Files:**
- Create: `bridge/security.py`, `tests/test_security.py`

**Interfaces:**
- Consumes: `BridgeError`.
- Produces:
  - `is_origin_allowed(origin: str | None, patterns: list[str]) -> bool` (fnmatch en minúsculas, sin `/` final; `"*"` permite todo; `None`/vacío → True).
  - `validate_network_target(ip: str, port: int, allowed_ports: list[int]) -> str` → IP normalizada o `BridgeError(..., 400)` con los mensajes del script: `"ip_address must be a literal IPv4 address."`, `"ip_address '<ip>' is not a private network address."`, `"ip_port <p> is not allowed. Allowed ports: <lista>."`.
  - `check_api_key(provided: str | None, expected: str) -> bool` (`hmac.compare_digest`; expected vacío → True).
  - `hash_password(pw: str) -> str` (`pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`, 260000 iteraciones), `verify_password(pw: str, stored: str) -> bool`.
  - `class LoginThrottle(max_attempts=5, lock_seconds=600, clock=time.monotonic)`: `is_locked(ip) -> bool`, `fail(ip) -> None`, `reset(ip) -> None`.
  - `ip_in_networks(ip: str, cidrs: list[str]) -> bool` (lista vacía → False; loopback siempre True).
  - `local_host_ok(host_header: str | None, port: int) -> bool` (solo `127.0.0.1:<port>` o `localhost:<port>`).

- [ ] **Step 1: Tests que fallan:** los casos probados sobre el script (`https://dimabe-odoo-grupo-los-lirios.odoo.com` ✓, `https://dimabe-odoo-grupo-los-lirios-staging-06072026-39556733.dev.odoo.com` ✓, `http://lirios.localhost` ✓, `https://evil.example.com` ✗, `None` ✓); destinos `8.8.8.8` ✗, `google.com` ✗, `192.168.1.10:22` ✗, `10.255.255.1:9100` ✓, `127.0.0.1:9100` ✗; password ok/ko; throttle bloquea al 5.º fallo y libera a los 600 s con reloj falso; `ip_in_networks("192.168.1.40", ["192.168.1.0/24"])` ✓; `local_host_ok("evil.com:3210", 3210)` ✗.
- [ ] **Step 2:** `pytest tests/test_security.py -v` → FAIL.
- [ ] **Step 3:** Implementar `bridge/security.py`.
- [ ] **Step 4:** → PASS. **Step 5:** Commit `feat: reglas de seguridad del puente`.

### Task 3: Bitácora SQLite

**Files:**
- Create: `bridge/journal.py`, `tests/test_journal.py`

**Interfaces:**
- Produces `class Journal(db_path: Path, clock=datetime.now)`:
  - `start() -> None` (crea esquema, WAL, hilo escritor), `stop() -> None`, `flush(timeout=5) -> None` (espera cola vacía; para tests).
  - `record(**fields) -> None` no bloqueante. Campos (tabla `events`, columnas del spec): `ts, day, kind, document_type, target_label, target_kind, printer_alias, origin, client_ip, bytes, duration_ms, result, http_status, message, job_id, test_mode, payload_kind, payload`. `ts`/`day` los pone el journal (`day` = fecha local).
  - `query_day(day: date, result: str | None = None, target: str | None = None, document_type: str | None = None, text: str | None = None, limit: int = 500, offset: int = 0) -> list[dict]` (orden descendente por ts; sin `payload`).
  - `get(event_id: int) -> dict | None` (incluye `payload` en hex).
  - `summary_day(day: date) -> dict`: `{"total", "ok", "error", "rejected", "by_hour": [24 ints de ok], "errors_by_hour": [24], "by_target": {label: {"ok":n,"error":n,"last_ts":...,"last_message":...}}}`.
  - `purge(retention_days: int) -> int`; `export_csv(day: date) -> str` (UTF-8 con BOM, `;` separador, sin payload); `size_info() -> {"bytes": int, "days": int}`.
  - `should_log_health(origin: str) -> bool` (máx. 1 por minuto por origen).
- Errores de SQLite en el hilo escritor se registran con `logging` y se descartan.

- [ ] **Step 1: Tests que fallan:** `test_record_and_query_day`, `test_filters_result_target_text`, `test_summary_hours_and_targets`, `test_purge_respects_retention` (clock falso: registros de hace 100 días se borran con 90), `test_export_csv_has_bom_and_header`, `test_health_throttled_per_minute`, `test_record_never_raises_when_db_broken` (ruta en carpeta sin permiso o db reemplazada por un directorio: `record()` no lanza y `flush()` termina).
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: bitácora diaria en SQLite`.

### Task 4: Intérprete ESC/POS para la vista previa

**Files:**
- Create: `bridge/escpos.py`, `tests/test_escpos.py`

**Interfaces:**
- Produces:
  - `looks_like_zpl(data: bytes) -> bool` (contiene `^XA` en los primeros 64 bytes tras quitar espacios).
  - `render_escpos(data: bytes, columns: int = 48) -> list[dict]`. Cada elemento es una línea `{"type":"text","text":str,"align":"left|center|right","bold":bool,"w":1-8,"h":1-8}`, un corte `{"type":"cut","mode":"full|partial"}`, una imagen `{"type":"image","png_base64":str,"width":int,"height":int}` o un comando desconocido `{"type":"unknown","hex":str}` (agrupando bytes consecutivos).
  - Comandos mínimos: `ESC @`, `ESC a n`, `ESC E n`, `GS ! n`, `ESC !` n (bits bold/doble), `ESC d n`/`LF`, `GS V m [n]`, `ESC M n` (ignorar), `ESC t n` (ignorar), `GS v 0 m xL xH yL yH d…` → PNG con Pillow, `ESC p` (ignorar en ticket). Texto decodificado como `cp858` con reemplazo.

- [ ] **Step 1: Tests que fallan:** con bytes construidos en el test: centrado + doble tamaño `1B 61 01 1D 21 11 "MESA 12" 0A` → línea center, w=2, h=2; negrita on/off; `1D 56 00` → cut full, `1D 56 01` → partial; raster 8×2 puntos → image width 8 height 2; bytes `1B 99` → unknown; `looks_like_zpl(b"^XA^FO50,50^FDHola^FS^XZ")` True. Además, la comanda real de `grupo_los_lirios` (copiar en `tests/fixtures/comanda.hex` el hex del panel de bitácora de la maqueta completado con un buffer generado por `buildPreparationEscPosHex`, o uno escrito a mano con los mismos comandos) se interpreta sin `unknown`.
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: intérprete ESC/POS para vista previa`.

### Task 5: Backends de impresión y captura del modo prueba

**Files:**
- Create: `bridge/backends.py`, `bridge/capture.py`, `tests/test_backends.py`, `tests/test_capture.py`

**Interfaces:**
- Consumes: `BridgeError`.
- Produces:
  - `@dataclass(frozen=True) Target`: `kind: Literal["usb","ip"]`, `printer_name: str = ""`, `ip: str = ""`, `port: int = 9100`; propiedad `label` (`nombre` o `ip:puerto`).
  - `class Backend(Protocol)`: `send_raw(target: Target, data: bytes, job_name: str) -> None`; `print_image(target: Target, image: bytes, paper_width_mm: int, job_name: str) -> None`; `list_printers() -> list[dict]` (claves `Name, DriverName, PortName, Type, Shared`); `queue_status(printer_name: str) -> str` (`ok|paused|offline|error|not_found`).
  - `class WindowsBackend` (usa `win32print`, `win32ui`, `PIL.ImageWin`, `socket`): RAW con `OpenPrinter/StartDocPrinter(1,(job,None,"RAW"))/StartPagePrinter/WritePrinter`, verifica bytes escritos, `AbortPrinter` si falla a mitad; red con `socket.create_connection(timeout=3)` + `settimeout(5)` + `sendall`; imagen escalada al área imprimible (`HORZRES`, `VERTRES`, `PHYSICALOFFSETX`) manteniendo proporción y paginando si la altura supera `VERTRES`; `queue_status` desde `GetPrinter(h, 2)["Status"]` (bits `PRINTER_STATUS_PAUSED`, `OFFLINE`, `ERROR`, `PAPER_OUT`→`error`); `send_raw`/`print_image` llaman a `queue_status` antes y lanzan `BridgeError("La impresora '<n>' está <estado> en Windows.", 500)` si no es `ok`; imagen hacia IP → `BridgeError(..., 400)` con el mensaje del script.
  - `@dataclass CapturedJob`: `id: int`, `ts: str`, `target_label: str`, `document_type: str`, `kind: Literal["raw","image","cashbox"]`, `data: bytes`.
  - `class CaptureStore(max_items=500)`: `add(target_label, document_type, kind, data) -> CapturedJob`, `list(target_label: str | None = None) -> list[CapturedJob]` (recientes primero), `get(id) -> CapturedJob | None`, `clear() -> None`, `targets() -> dict[str, int]`.
  - `class VirtualBackend(store: CaptureStore, settings_provider: Callable[[], Settings], sleep=time.sleep)`: implementa `Backend`; aplica demora `test_delay_s` y la respuesta simulada (`no_paper` → `BridgeError("Sin papel (simulado).")`, `offline` → `"Impresora desconectada (simulado)."`, `timeout` → duerme `test_delay_s` y lanza `"Tiempo de espera agotado (simulado)."`, `one_in_three` → falla la 3.ª, 6.ª…); `list_printers()` devuelve las virtuales vistas (`Type: "Virtual"`); `queue_status` → `ok`.

- [ ] **Step 1: Tests que fallan:** `WindowsBackend` con un `win32print` falso inyectado (parámetro `win32print=None` en el constructor para tests): RAW escribe exactamente los bytes y cierra doc/página; cola pausada → BridgeError sin escribir; `WritePrinter` parcial → error y `AbortPrinter` llamado; red: servidor TCP local de prueba en `127.0.0.1` recibe los bytes (el backend no valida IP privada: eso es del servicio). `VirtualBackend`: captura RAW/imagen/cajón; cada modo simulado; `one_in_three` falla exactamente en la 3.ª.
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: backends Windows y virtual`.

### Task 6: Servicio de impresión

**Files:**
- Create: `bridge/service.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces `class PrintService(settings_provider: Callable[[], Settings], real: Backend, virtual: Backend, journal: Journal, clock=time.monotonic)`:
  - `parse_target(payload: dict) -> Target` (IP gana; valida con `validate_network_target`; sin destino → `BridgeError("A printer target is required (printer_name for USB or ip_address for network).", 400)`).
  - `print_job(payload: dict, origin: str | None, client_ip: str, source: str = "api") -> dict` → `{result, status, message, test_mode, duplicate?}`; contenido en orden `raw_base64` → `raw_hex` → `receipt` usando solo claves con valor no vacío; mensajes de éxito idénticos al script (`"Raw bytes sent to '<label>'."`, `"Printed on '<label>'."`).
  - `cashbox(payload: dict, origin, client_ip, source="api") -> dict` (`"Cash drawer pulse sent to '<label>'."`).
  - `reprint(event_id: int, target: Target | None = None) -> dict` (reenvía el payload guardado; error 404 si no hay payload).
  - Usa `virtual` si `settings.test_mode`; lock por `target.label` (`threading.Lock` en dict protegido); dedupe por `job_id` 300 s; registra en la bitácora todo intento (ok/error/duplicate) con `duration_ms`, guardando payload según `store_payload`/`store_images` (en modo prueba siempre).

- [ ] **Step 1: Tests que fallan:** destinos y prioridades; `raw_base64: ""` con `receipt` válido → usa la imagen (bug M3 del script); dedupe devuelve `duplicate: true` sin segunda escritura y expira a los 300 s (clock falso); modo prueba nunca toca `real`; **concurrencia (Review Focus 3):** backend falso que duerme 0,2 s y registra intervalos: 2 trabajos al mismo destino no se solapan, 2 a destinos distintos sí; la bitácora recibe un registro por intento.
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: servicio de impresión con dedupe y locks`.

### Task 7: API compatible y app local

**Files:**
- Create: `bridge/api.py`, `bridge/server.py` (solo `AppContext` y `create_local_app` en esta tarea), `tests/test_api.py`

**Interfaces:**
- Consumes: `PrintService`, `Journal`, `security`.
- Produces:
  - `@dataclass AppContext`: `settings: Settings`, `save: Callable[[Settings], None]`, `journal: Journal`, `service: PrintService`, `captures: CaptureStore`, `real: Backend`, `monitor: "PrinterMonitor | None" = None`, `csrf_token: str` (aleatorio por arranque).
  - `create_local_app(ctx: AppContext) -> Flask` registra el blueprint API y los del panel (Tasks 8–9 los añaden). `MAX_CONTENT_LENGTH = 20 MB` con manejador 413 JSON.
  - Blueprint `api`: rutas de la tabla del spec. Antes de cada request de `/api/*`: origen (403 `"Origin not allowed by the bridge."` + bitácora `rejected`), API key (401 `"Invalid bridge API key."`). CORS: `Access-Control-Allow-Origin` = origen solo si permitido, `Vary: Origin`, `Allow-Headers: Content-Type, X-Dimabe-Bridge-Key`, `Allow-Methods: GET, POST, OPTIONS`, `Allow-Private-Network: true`, `Max-Age: 600`; OPTIONS → 204 sin cuerpo. `BridgeError` → su status; otra excepción → 500. `/api/health` incluye `version`, `test_mode`, `listen_url`.

- [ ] **Step 1: Tests que fallan** (Flask test client, backends falsos): los 11 casos que se corrieron contra el script (preflight permitido/staging/local → 204 con Allow-Origin; sitio ajeno health y cajón text/plain → 403 sin Allow-Origin; sin Origin → 200; IP pública, DNS, puerto 22 → `result:false`; IP privada 9100 → llega al backend; **nombre «Impresora Cocina Ñandú» llega intacto al backend y a la bitácora** (Review Focus 2); **4 MB → ok y 21 MB → 413 JSON** (Review Focus 1)); API key incorrecta → 401; **modo prueba persistido:** `save_settings` con `test_mode=True`, nuevo `AppContext` cargado desde disco → `/api/health` y `/api/print` devuelven `test_mode: true` (Review Focus 5).
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: API compatible con el POS`.

### Task 8: API del panel y autenticación

**Files:**
- Create: `bridge/panel_api.py`, `tests/test_panel_api.py`
- Modify: `bridge/server.py` (agregar `create_remote_app`)

**Interfaces:**
- Consumes: `AppContext`, `security`, `Journal`, `CaptureStore`, `render_escpos`, `looks_like_zpl`.
- Produces blueprint `panel_api` en `/panel/api` (JSON):
  - `GET status` → `{service:{version, listen_url, since, api_key_set, remote_enabled, test_mode}, today: summary_day, printers: monitor.snapshot(), attention: [{level, title, detail, link}], last_rejected, journal: size_info}`.
  - `GET printers`; `POST printers/test` `{target, kind: "text|connection|cashbox|label"}`.
  - `GET journal?day=YYYY-MM-DD&result=&target=&document_type=&q=&offset=`; `GET journal/<id>`; `GET journal/<day>.csv`; `POST journal/<id>/reprint`; `POST journal/purge-all`.
  - `GET test/jobs?target=`; `GET test/jobs/<id>/preview` → `{kind, lines|zpl_text|png_base64}`; `POST test/clear`; `POST test/sample` `{sample: "boleta|comanda|etiqueta", target}` (pasa por `service.print_job` con `source="panel"`); `POST test/forward` `{job_id, target}`.
  - `GET settings` (sin hash ni api_key en claro: `api_key_set: bool`); `POST settings` (valida y guarda; cambia puerto → `restart_required: true`; al encender el panel remoto ejecuta `netsh advfirewall firewall add rule name="Dimabe Print Bridge panel" dir=in action=allow protocol=TCP localport=<puerto>` y devuelve `firewall: "ok" | "needs_admin"` más el comando para correrlo como administrador); `POST settings/api-key/generate` → nueva clave (se muestra una vez); `POST settings/remote-password` `{user, password}`.
  - Auth: en la app local, todo request valida `local_host_ok`; los `POST` exigen header `X-CSRF-Token == ctx.csrf_token` y `Origin` igual a `http://127.0.0.1:<port>` o `http://localhost:<port>` (o ausente). En la app remota: sesión Flask (`SECRET_KEY` aleatoria persistida en `settings.json` como `session_secret`), `ip_in_networks(remote_addr, remote_networks)`, `LoginThrottle`, cookie `HttpOnly`, `SameSite=Strict`, 8 h; `POST /panel/login` y `POST /panel/logout`.
  - `create_remote_app(ctx) -> Flask`: registra solo los blueprints del panel; `/api/print` y `/api/cashbox` → 404.

- [ ] **Step 1: Tests que fallan:** `Host: evil.com` → 403; POST sin token → 403; con token → 200; remoto: sin sesión → 401 JSON / redirección a login para HTML; login correcto → 200; 5 fallos → 429 «bloqueado 10 minutos»; IP fuera de red → 403; `/api/print` en remoto → 404; preview de un trabajo ESC/POS capturado devuelve `lines`; `settings` nunca devuelve el hash ni la api key.
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Commit `feat: API del panel con autenticación local y remota`.

### Task 9: Panel (HTML/CSS/JS)

**Files:**
- Create: `bridge/panel_views.py`, `bridge/templates/*.html`, `bridge/static/panel.css`, `bridge/static/panel.js`, `bridge/static/icons.svg`, `bridge/static/fonts/` (IBM Plex Sans 400/500/600/700 y Mono 400/500 woff2, descargadas una vez de Google Fonts y versionadas con su licencia OFL), `bridge/static/samples/*`, `tests/test_panel_views.py`

**Interfaces:**
- Consumes: `panel_api` (rutas JSON), `ctx.csrf_token` (inyectado en `base.html` como `<meta name="csrf-token">`).
- Produces rutas HTML: `/panel` (Estado), `/panel/impresoras`, `/panel/bitacora`, `/panel/prueba`, `/panel/configuracion`, `/panel/login`; `/` redirige a `/panel`.

- [ ] **Step 1:** Cargar la skill `impeccable` (modo Operate) y su `craft-floor.md` antes de escribir la UI. Fuente visual: `maquetas/panel-completo.html` (tokens, tipografía, pills, tablas, banner de modo prueba, layout con barra lateral). Pasar los tokens a `panel.css` sin cambiar valores, salvo `--ink-3:#5b655e`.
- [ ] **Step 2: Test que falla** `tests/test_panel_views.py`: cada ruta responde 200 con su `<h2>` («Estado», «Impresoras», «Bitácora», «Modo prueba», «Configuración») y el meta CSRF; ninguna plantilla referencia `http://` o `https://` externos (regex sobre el HTML renderizado y `panel.css`).
- [ ] **Step 3:** FAIL. **Step 4:** Implementar plantillas Jinja (contenido inicial vacío con estados de carga) y `panel.js` (fetch a `/panel/api/*`, refresco cada 5 s en Estado y Bitácora del día actual con `document.visibilityState`, confirmación antes de acciones con efecto en papel, mensajes de error con causa y cómo seguir, tema claro/oscuro con `prefers-color-scheme` y selector guardado en `localStorage`).
- [ ] **Step 5:** PASS. **Step 6:** Verificación visual: levantar la app en modo prueba con datos de ejemplo (`python main.py --no-tray --demo` carga 400 eventos ficticios del día y 3 trabajos capturados en la data dir de desarrollo), abrir cada pantalla a 1440 y 1024 px de ancho con Playwright (`pip install playwright` + `playwright install chromium` en el venv), capturar en `docs/capturas/`, comparar con la maqueta y corregir. Correr `impeccable detect --json bridge/templates bridge/static/panel.css` y resolver lo que no sea cromo de ventana.
- [ ] **Step 7:** Commit `feat: panel web para TI`.

### Task 10: Monitor, servidor, bandeja y arranque

**Files:**
- Create: `bridge/monitor.py`, `bridge/tray.py`, `main.py`, `tests/test_monitor.py`, `tests/test_server.py`
- Modify: `bridge/server.py` (agregar `ServerRunner`)

**Interfaces:**
- Produces:
  - `class PrinterMonitor(real: Backend, journal: Journal, settings_provider, interval_s=60, connect=socket.create_connection)`: `start()`, `stop()`, `refresh_now() -> None`, `snapshot() -> list[dict]` (`{label, kind, status: ok|warn|error|test|off, status_text, today_ok, today_error, last_ts, last_message}`); IP a sondear = destinos IP en la bitácora de los últimos 7 días, conexión de 1 s.
  - `class ServerRunner(ctx)`: `start() -> None` (hilos waitress local y, si corresponde, remoto, `threads=8`), `stop()`, `restart()`; `state` (`running|port_busy|stopped`) y `busy_by: str` (nombre y PID del proceso dueño del puerto vía `psutil`-less: `netstat -ano` + `tasklist /FI "PID eq n"`); reintenta cada 10 s si el puerto está ocupado.
  - `tray.run_tray(ctx, runner, on_quit) -> bool`: ícono 64×64 generado con Pillow (impresora estilizada) en verde `#1f5c46`, ámbar `#9a5b00`, morado `#5b3fa8`, gris `#6b756e` según `runner.state`, `settings.test_mode` y errores de los últimos 30 min; menú del spec; notificación cuando `port_busy`.
  - `main.py`: `--no-tray`, `--demo`, `--port`; configura logging rotativo; aplica `installer-seed.json`; arranca journal (purga), monitor, runner y bandeja; instancia única con mutex de Windows `Global\DimabePrintBridge`.

- [ ] **Step 1: Tests que fallan:** monitor marca `error` un destino IP cuyo connect falso lanza y `ok` el que conecta; `ServerRunner` con puerto ocupado por un socket del test queda en `port_busy` y pasa a `running` al liberarlo (intervalo de reintento inyectable a 0,1 s).
- [ ] **Step 2:** FAIL. **Step 3:** Implementar. **Step 4:** PASS. **Step 5:** Prueba manual: `python main.py --port 3299` y repetir con curl los casos de la Task 7 contra el proceso real; detener. **Step 6:** Commit `feat: bandeja, monitor y arranque`.

### Task 11: Empaquetado, firma e instalador

**Files:**
- Create: `DimabePrintBridge.spec`, `installer/DimabePrintBridge.iss`, `installer/build.ps1`, `installer/crear-certificado.ps1`, `installer/instalar-certificado.ps1`, `installer/dimabe-print-bridge.cer`, `bridge/static/icon.ico` (generado con Pillow desde el mismo dibujo del ícono)

**Interfaces:**
- `crear-certificado.ps1`: `New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Dimabe Ltda" -KeyAlgorithm RSA -KeyLength 3072 -NotAfter (Get-Date).AddYears(5) -CertStoreLocation Cert:\CurrentUser\My`; exporta `.cer` a `installer\dimabe-print-bridge.cer` y `.pfx` (clave pedida con `Read-Host -AsSecureString`) a `%USERPROFILE%\Documents\Desarrollo\Trabajo\certificados\dimabe-print-bridge.pfx`; imprime la huella. Lo corre Fabián (pide clave); si se ejecuta sin interacción, toma la clave de `$env:DIMABE_PFX_PASSWORD`.
- `instalar-certificado.ps1` (admin): importa el `.cer` a `Cert:\LocalMachine\Root` y `Cert:\LocalMachine\TrustedPublisher`.
- `build.ps1`: tests → PyInstaller (`--noconfirm --clean`) → firma `dist\DimabePrintBridge.exe` → ISCC → firma `release\DimabePrintBridge-Setup-<versión>.exe`; firma con `Set-AuthenticodeSignature -Certificate (cert por huella en CurrentUser\My) -TimestampServer http://timestamp.digicert.com -HashAlgorithm SHA256`; versión leída de `bridge/__init__.py` y pasada a ISCC con `/DMyAppVersion=`.
- `.iss`: modelado sobre `serialport_api/installer/SerialPortAPI.iss`, con: `AppId` nuevo, instala en `{autopf}\DimabePrintBridge`, crea `{commonappdata}\DimabePrintBridge` con permiso `users-modify`, tareas `autostart` (marcada), `replaceold` (marcada si existe la tarea «Dimabe Print Bridge» de PowerShell: la elimina y mata `pwsh/powershell` cuyo comando contiene `dimabe_print_bridge.ps1`), `remotepanel` (no marcada), página custom con clave API y puerto que escribe `{commonappdata}\DimabePrintBridge\installer-seed.json`; tarea programada `DimabePrintBridge` igual que SerialPortAPI; incluye `instalar-certificado.ps1` y el `.cer` en `{app}\certificado\`; desinstalar pregunta si borrar la bitácora.

- [ ] **Step 1:** `winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements` (autorizado por Fabián).
- [ ] **Step 2:** Correr `installer\crear-certificado.ps1` (pide a Fabián la clave del `.pfx` con `! pwsh -File installer\crear-certificado.ps1` en su terminal) y guardar la huella en `installer\cert-thumbprint.txt`.
- [ ] **Step 3:** Escribir spec de PyInstaller, `.iss`, `build.ps1` y los dos scripts.
- [ ] **Step 4:** `powershell -ExecutionPolicy Bypass -File installer\build.ps1` → genera y firma ambos `.exe`; `Get-AuthenticodeSignature release\*.exe` muestra `SignerCertificate.Subject = CN=Dimabe Ltda`.
- [ ] **Step 5:** Smoke test del ejecutable sin instalar: `dist\DimabePrintBridge.exe --port 3299 --no-tray` (con `DIMABE_BRIDGE_DATA_DIR` temporal), `curl /api/health` → `version 1.0.0`; detener.
- [ ] **Step 6:** Commit `build: ejecutable firmado e instalador`.

### Task 12: Documentación

**Files:**
- Create: `README.md`, `BUILD.md`, `MANUAL_TI.md`

- [ ] **Step 1:** README (qué es, endpoints, datos, desarrollo), BUILD (requisitos, certificado, `build.ps1`), MANUAL_TI (instalar desde Odoo, certificado y aviso de SmartScreen, primer arranque, panel y cada pantalla con capturas de `docs/capturas/`, panel desde la red y firewall, modo prueba, problemas frecuentes, **lista de chequeo con hardware real**: boleta BIXOLON por USB, comanda por IP, cajón, etiqueta Zebra, impresora apagada, cola pausada, modo prueba, reinicio de Windows con inicio automático).
- [ ] **Step 2:** Commit `docs: README, build y manual de TI`.

### Task 13: Descarga del instalador desde Odoo

**Files (repo `grupo_los_lirios`, worktree desde `origin/PRD`):**
- Create: `dimabe_pos/models/dimabe_print_bridge_release.py`, `dimabe_pos/controllers/print_bridge_download.py`, `dimabe_pos/views/dimabe_print_bridge_release_views.xml`, `dimabe_pos/tests/test_print_bridge_release.py`
- Modify: `dimabe_pos/models/__init__.py`, `dimabe_pos/controllers/__init__.py`, `dimabe_pos/tests/__init__.py`, `dimabe_pos/security/ir.model.access.csv`, `dimabe_pos/__manifest__.py` (vista + versión `19.0.7.35.0`), `dimabe_pos/views/pos_config_feature_views.xml` (botón en la sección «Puente de impresión Dimabe»), `dimabe_pos/static/src/app/components/bridge_diagnostics_popup/bridge_diagnostics_popup.{js,xml}`, `tools/dimabe_print_bridge.md`

**Interfaces:**
- Modelo `dimabe.print.bridge.release` (`_order = "upload_date desc"`): `name` Char requerido (versión), `installer` Binary `attachment=True` requerido, `installer_filename` Char, `certificate` Binary `attachment=True`, `certificate_filename` Char, `notes` Text, `is_current` Boolean, `upload_date` Datetime default now, `active` Boolean default True. Todos con `help`. Al guardar `is_current=True` se desmarcan los demás (`write` sobre `search([("is_current","=",True),("id","not in",self.ids)])`, sin búsquedas en bucle). `@api.model get_current_info()` → `{version, filename, has_certificate, download_url, certificate_url}` o `False`; lo llama el popup vía ORM.
- Acceso: `group_pos_user` lectura; `group_pos_manager` todo.
- Controlador: `GET /dimabe_pos/print_bridge/download` y `/dimabe_pos/print_bridge/certificate`, `auth="user"`, verifica `has_group("point_of_sale.group_pos_user")`, devuelve el adjunto vigente con `request.env["ir.binary"]._get_stream_from(...)` y `as_attachment=True`; sin vigente → 404 con texto «No hay un instalador del Print Bridge cargado en Odoo».
- Menú «Punto de venta › Configuración › Print Bridge (instalador)» con acción lista/formulario (`<list>`).
- Popup: botón «Descargar Print Bridge» siempre visible; si el `health` falla, un aviso destacado «El puente no está instalado o no está corriendo en este equipo» con el botón; si `health.version` < vigente, «Hay una versión más nueva (x.y.z)». Comparación de versiones numérica por partes.

- [ ] **Step 1:** Crear worktree: `git -C grupo_los_lirios worktree add -b feat/print-bridge-descarga-odoo ../_worktrees/print-bridge-descarga-23102026 origin/PRD`.
- [ ] **Step 2: Tests que fallan** (`TransactionCase`, `HttpCase` para el controlador): marcar una vigente desmarca la anterior; usuario POS no puede crear (AccessError); `get_current_info()` False sin vigentes; descarga sin vigente → 404; con vigente → 200 y `Content-Disposition` con el nombre del archivo; usuario sin grupo POS → 403.
- [ ] **Step 3:** Respaldo previo según CLAUDE.md, MariaDB no aplica. Correr los tests sobre una copia de PRD (`<db>_test`) con `--no-http`: `python odoo-bin -c "C:\Users\Fabian\Documents\Desarrollo\Trabajo\confs\odoo-lirios-staging-new.conf" -d <db>_test -u dimabe_pos --test-tags /dimabe_pos:TestPrintBridgeRelease --stop-after-init --no-http` → FAIL.
- [ ] **Step 4:** Implementar modelo, seguridad, vistas, controlador, botones.
- [ ] **Step 5:** Mismo comando → PASS; además correr el resto de tests de `dimabe_pos` y comparar con PRD sin el cambio.
- [ ] **Step 6:** Actualizar `tools/dimabe_print_bridge.md` apuntando a la app y a la descarga.
- [ ] **Step 7:** Commit y push solo de la rama; preguntar antes de subir a PRD/staging.

---

## Self-review

- Cobertura del spec: API compatible (T7), mejoras 400/413/hilos/locks/timeouts/job_id/cola (T5–T7), bitácora (T3, T8), modo prueba (T4–T6, T8–T9), panel 7 pantallas (T9; bandeja e instalador T10–T11), panel remoto + firewall (T8; regla de firewall: `POST settings` con remoto encendido intenta `netsh advfirewall firewall add rule name="Dimabe Print Bridge panel" dir=in action=allow protocol=TCP localport=<puerto>` y devuelve `firewall: ok|needs_admin` con el comando), datos en ProgramData y seed (T1, T11), firma (T11), descarga Odoo (T13), docs (T12).
- Review Focus: los 5 casos tienen test en T3, T6 y T7.
