# Resiliencia de la impresión: plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (elegido: ejecución directa). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Que ninguna comanda se pierda ni se duplique y que toda caída se note y se avise (14 mejoras del spec).

**Architecture:** El puente gana una cola persistente (`cola.sqlite`), impresión de respaldo, sondeo de estado ESC/POS, `/api/status`, un vigilante, un modo servicio de Windows con bandeja separada y un actualizador verificado. El POS gana un único camino de impresión de comandas con `job_id`, una cola local con reintento y plan B, un indicador del puente y un latido hacia Odoo; Odoo guarda el latido, alerta por correo y valida la configuración.

**Tech Stack:** Python 3.12 (Flask, waitress, pywin32, sqlite3), Inno Setup, GitHub Actions; Odoo 19 (`dimabe_pos`, OWL, `mail`), Playwright para el E2E.

**Spec:** `docs/superpowers/specs/2026-10-23-resiliencia-impresion-design.md`

## Global Constraints

- Puente: rama `feat/resiliencia` → PR a `main` (protegido). `__version__ = "1.1.0"`.
- Odoo: rama `feat/pos-impresion-resiliente` desde `feat/print-bridge-descarga-odoo`, worktree `proyectos/_worktrees/pos-resiliente-23102026`; `dimabe_pos` 19.0.7.36.0. Solo `_inherit`; `<list>`; `help` en cada campo; sin `search` en bucles.
- Códigos de error del puente: `printer_unreachable`, `printer_busy`, `printer_no_paper`, `printer_cover_open`, `printer_offline`.
- Encolables: `document_type` en (`kitchen`, `preparation`) salvo `queue_on_failure: false`. Boletas nunca.
- Cola del puente: reintento 10 s → ×2 → máx 60 s; vence a las 2 h; 200 por impresora.
- Cola del POS: `localStorage` clave `dimabe_pos.print_queue.<config_id>`; reintento cada 15 s; plan B tras 3 fallas o 60 s sin puente.
- Latido POS→Odoo cada 60 s; cron cada 5 min; umbrales: sin latido 10 min, puente caído 5 min, pendientes 10 min; una alerta por problema por hora; correos en `dimabe_pos.bridge_alert_emails`.
- Actualización: `update_mode` `auto|notify|off` (por defecto `auto`), ventana 03:00–05:00, sin impresiones en 30 min, huella fija `671F9489819782DD56B2BDE588BEEC030BF41018` (leída de `installer/cert-thumbprint.txt` empaquetado), revisión cada 6 h.
- Vigilante: cada 30 s, timeout 5 s, 3 fallas → salir con código 1.
- Respetar CLAUDE.md: Odoo solo en 8070; pruebas con `--no-http` (HttpCase con Odoo de prueba detenido); respaldo antes de `-u`.

## Review Focus

1. **Trabajo encolado y POS que reintenta con el mismo `job_id`:** el puente responde `queued`/`duplicate`, nunca imprime dos veces → test en B-T2.
2. **Puente reiniciado con trabajos en la cola:** al arrancar los retoma y no los pierde → test en B-T3.
3. **Recarga del navegador con comandas pendientes en la cola del POS:** siguen ahí y se reintentan → E2E en O-T6.
4. **Actualización con instalador de otra firma o hash distinto:** no se instala y queda en la bitácora → test en B-T5.
5. **Ráfaga de latidos con el puente caído:** una sola alerta por hora por problema → test en O-T2.

---

### B-T1: Cola persistente (`bridge/outbox.py`)
**Files:** Create `bridge/outbox.py`, `tests/test_outbox.py`; Modify `bridge/config.py` (`outbox_path()`).
**Produces:** `class Outbox(db_path, clock=time.time)`: `add(job_id, target: Target, document_type, data: bytes, fallback: Target|None) -> int` (raises `BridgeError(503, code="queue_full")` si hay 200 para el destino; si `job_id` ya está pendiente devuelve su id), `due(now=None) -> list[QueuedJob]`, `mark_done(id)`, `mark_failed(id, message)` (programa siguiente intento: 10·2^(n-1) s, máx 60; vence a las 2 h → estado `expired`), `discard(id)`, `retry_now(id)`, `pending(target_label=None) -> list[dict]`, `counts() -> {"total", "by_target"}`, `expired_since(ts) -> list[dict]`. Escritura síncrona (WAL).
- [ ] Tests: agregar/listar/contar; tope 200; mismo job_id no duplica; backoff 10/20/40/60/60; vencimiento a 2 h; persistencia tras reabrir el archivo; descartar.
- [ ] RED → implementar → GREEN → commit `feat: cola persistente de trabajos`.

### B-T2: Respaldo, cola y estado ESC/POS en el servicio
**Files:** Modify `bridge/service.py`, `bridge/backends.py`, `bridge/errors.py`; Test `tests/test_resilience_service.py`, `tests/test_backends.py`.
**Produces:** `WindowsBackend.network_status(target) -> "ok"|"no_paper"|"cover_open"|"unknown"` (DLE EOT 2 y 4, 500 ms, recuerda 1 h los que no responden); `send_raw` de red lo usa salvo `document_type == label` (nuevo parámetro opcional `document_type` en `send_raw`). `PrintService.print_job` acepta `fallback_target` (dict como el destino), `queue_on_failure` (bool). Flujo ante error con código de los encolables: respaldo (con cabecera ESC/POS «RESPALDO DE <label>») → si falla o no hay, `Outbox.add` → respuesta `{"result": true, "status": "queued", "queued": true, "message": "La impresora <x> no responde: el trabajo quedó en cola y se imprimirá cuando vuelva."}`. `fallback_used: true` cuando sale por respaldo. Boletas: error como hoy.
- [ ] Tests: respaldo usado con cabecera; respaldo caído → cola; boleta no se encola; `queue_on_failure:false` no encola; mismo job_id ya en cola → `duplicate`; sin papel (fake socket que responde 0x60 a DLE EOT 4) → `printer_no_paper` → cola; impresora que no responde estado → se imprime y no se vuelve a consultar en 1 h.
- [ ] RED → GREEN → commit `feat: respaldo, cola y estado real de impresoras de red`.

### B-T3: Trabajador de la cola y panel
**Files:** Create `bridge/outbox_worker.py`; Modify `bridge/server.py` (`AppContext.outbox`, `build_context`), `main.py`, `bridge/panel_api.py` (`GET /panel/api/queue`, `POST /panel/api/queue/<id>/retry`, `POST .../discard`), `bridge/templates/estado.html`/`impresoras.html`, `bridge/static/panel.js`, `bridge/panel_api.py::_attention` (cola y vencidos); Test `tests/test_outbox_worker.py`.
**Produces:** `OutboxWorker(outbox, service, interval=2)`: `start/stop/run_once()`; usa el lock por destino (`service`), registra en bitácora (`kind="queue"`).
- [ ] Tests: `run_once` imprime los vencidos y los marca; falla → reprogramado; trabajo pendiente de antes de «reiniciar» (nuevo Outbox sobre el mismo archivo) se imprime; atención muestra cola > 0.
- [ ] RED → GREEN → commit `feat: reintento automático de la cola y vista en el panel`.

### B-T4: `GET /api/status`
**Files:** Modify `bridge/api.py`; Test `tests/test_api.py`.
- [ ] Test: misma regla de origen/clave; campos `version`, `test_mode`, `update`, `queue`, `printers`, `recent_errors`.
- [ ] RED → GREEN → commit `feat: /api/status para el POS`.

### B-T5: Actualizador
**Files:** Create `bridge/updater.py`, `installer/rollback-check.ps1`; Modify `bridge/config.py` (`update_mode`, `update_window`), `bridge/panel_api.py` (ajuste y estado), `configuracion.html`, `panel.js`, `DimabePrintBridge.spec` (empaqueta `installer/cert-thumbprint.txt`, `rollback-check.ps1`); Test `tests/test_updater.py`.
**Produces:** `Updater(settings_provider, journal, fetch=..., download=..., verify=..., run_installer=..., clock=...)`: `check() -> {"available", "latest", "current"}`, `maybe_install(now)`, `state()`. `verify_signature(path) -> (thumbprint|None, status)` vía PowerShell `Get-AuthenticodeSignature`; acepta solo huella fija; SHA-256 contra `digest` del asset si viene. Instalación: `Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1`, guardando el instalador anterior en `updates/`; programa `rollback-check.ps1` (tarea SYSTEM de una vez a +5 min).
- [ ] Tests (todo inyectado): versión igual → nada; nueva + `notify` → disponible sin instalar; `auto` fuera de ventana → no; dentro de ventana con impresión hace 10 min → no; firma de otra huella → rechazado + bitácora; hash distinto → rechazado; todo bien → `run_installer` llamado.
- [ ] RED → GREEN → commit `feat: actualización verificada del puente`.

### B-T6: Servicio de Windows, bandeja aparte y vigilante
**Files:** Create `bridge/winservice.py`, `bridge/watchdog.py`, `bridge/tray_client.py`; Modify `main.py` (modos `--service`, `--tray`), `bridge/tray.py` (puede usar un cliente HTTP en vez del contexto), `bridge/server.py` (token local); Test `tests/test_watchdog.py`, `tests/test_tray_client.py`.
**Produces:** `Watchdog(check=callable, on_fail=callable, interval=30, failures=3)`; `local_token_path()` y `ensure_local_token() -> str` (lo usa `AppContext.csrf_token`); `TrayClient(port, token)` con `status()`, `set_test_mode(bool)`, `restart()`; servicio `DimabePrintBridgeService(win32serviceutil.ServiceFramework)` que corre el núcleo (todo menos bandeja) y para limpio en `SvcStop`.
- [ ] Tests: vigilante llama `on_fail` a la 3.ª falla seguida y se resetea con un éxito; token persistente entre arranques; `TrayClient` contra la app Flask de prueba.
- [ ] RED → GREEN → commit `feat: modo servicio, bandeja separada y vigilante`.

### B-T7: Instalador 1.1, CI de PR y publicación
**Files:** Modify `installer/DimabePrintBridge.iss` (instala y arranca el servicio con recuperación `sc failure ... restart/5000`, bandeja en `HKLM\...\Run` con `--tray`, quita tarea `DimabePrintBridge` de 1.0, para/arranca el servicio al actualizar, soporta `/UPDATE=1` sin preguntas), `bridge/__init__.py` (1.1.0), `README.md`, `MANUAL_TI.md`; Create `.github/workflows/ci.yml` (pytest en `pull_request`).
- [ ] Build local; instalar no se prueba en esta máquina (requiere UAC) — se valida servicio con `main.py --service debug` y la bandeja con `--tray` contra el núcleo.
- [ ] Commit, push de `feat/resiliencia`, abrir PR.

### O-T1: Configuración más segura (Odoo)
**Files:** Modify `dimabe_pos/models/pos_printer.py` (`dimabe_backup_printer_id`, constraint IP única por POS), `dimabe_pos/models/pos_config_print_bridge.py` o nuevo `pos_config_bridge_health.py` (`dimabe_uncovered_category_names` compute), vistas `pos_printer_views.xml` (botón probar con `bridge_printer_field`, respaldo), `pos_config_feature_views.xml` (aviso categorías); Test `tests/test_pos_printer_resilience.py`.
- [ ] Tests: IP repetida → ValidationError; respaldo no puede ser ella misma ni de otro tipo; aviso lista categorías sin impresora y desaparece con impresora general.
- [ ] RED → GREEN → commit.

### O-T2: Latido, tablero y alertas (Odoo)
**Files:** Create `dimabe_pos/models/pos_config_bridge_health.py`, `dimabe_pos/data/bridge_health_cron.xml`, `dimabe_pos/data/bridge_alert_mail.xml` (plantilla); Modify `views/pos_config_board_views.xml`, `__manifest__.py` (`mail` ya disponible vía point_of_sale), seguridad si aplica; Test `tests/test_bridge_health.py`.
**Produces:** campos en `pos.config`: `dimabe_bridge_last_seen`, `dimabe_bridge_state` (`ok|warn|down|test|unknown`), `dimabe_bridge_version`, `dimabe_bridge_pending`, `dimabe_bridge_detail`, `dimabe_bridge_update_available`, `dimabe_bridge_alert_log` (JSON interno). `@api.model dimabe_bridge_heartbeat(config_id, data)` (usuario POS, solo su config abierta). `_cron_dimabe_bridge_alerts()`.
- [ ] Tests: latido guarda campos; cron: sin latido 11 min con sesión abierta → alerta (mail.mail creado al parámetro + mensaje en chatter); segunda pasada misma hora → no repite; puente caído 6 min → alerta; sin sesión abierta → nada.
- [ ] RED → GREEN → commit.

### O-T3: Camino único de impresión de comandas (POS)
**Files:** Create `static/src/app/utils/printing/preparation_pipeline.js`; Modify `utils/printer/dimabe_bridge_printer.js` (`sendRawHex(hex, {jobId, fallbackTarget, queueOnFailure})`, `job_id` en todos los POST y reutilizado en reintentos internos), `store/pos_store_comanda_patch.js` (`dimabePrintPreparationReceipts(order, receipts, meta, {reprint})` → usa pipeline), `screens/dining_order_screen/dining_order_screen.js` y `operation_menu_screen.js` y `comanda_preview_screen.js` (llaman al método del store; tras el envío completo `order.updateLastOrderChange()` cuando se usó `byPassPrint`).
**Produces:** `printPreparationReceipts(pos, receipts, meta, {reprint}) -> {printed: [...], queued: [...], failed: [...]}`; cada trabajo `{jobId, printerId, label, hex, receiptLabel}`; `jobId = crypto.randomUUID()` por (receipt, impresora).
- [ ] Verificación: E2E O-T6.
- [ ] Commit.

### O-T4: Cola del POS y plan B
**Files:** Create `static/src/app/utils/printing/print_queue.js`, componente `components/print_queue_dialog/*`; Modify `store/pos_store_comanda_patch.js` (inicia la cola, reintento 15 s), `screens/dining_order_screen/*.xml/js` (aviso «comanda sin imprimir»).
**Produces:** `class PosPrintQueue(storage, configId)`: `add(job)`, `list()`, `forOrder(uuid)`, `remove(jobId)`, `markAttempt(jobId, error)`; persistencia en `localStorage`.
- [ ] Commit.

### O-T5: Indicador y latido (POS)
**Files:** Create `components/bridge_status_indicator/*` (registro como `main_components` igual que `dimabe_global_exit`); Modify store (poll `/api/status` 30 s, latido 60 s a `pos.config.dimabe_bridge_heartbeat`).
- [ ] Commit.

### O-T6: E2E y cierre
- [ ] Backup DB, `-u dimabe_pos`, tests Odoo con Odoo de prueba detenido, reiniciar Odoo y puente 1.1.0 (modo prueba), Playwright: (1) comanda con impresora simulada «desconectada» → POS reintenta, sin duplicados; (2) respaldo; (3) puente detenido → cola del POS, recarga, vuelve el puente → se imprime una vez; (4) latido visible en «Estado de cajas»; (5) cron alerta una vez.
- [ ] Commit, push de la rama, revisión final (subagente Sonnet), arreglos, informe.
