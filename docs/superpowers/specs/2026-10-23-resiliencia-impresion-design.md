# Resiliencia de la impresión (POS + Print Bridge): diseño

Fecha: 23-10-2026 · Pedido de Fabián: «hazlo todo» sobre las 14 mejoras propuestas para evitar caídas.

## Intención

Que una caja nunca se quede sin imprimir sin que nadie se entere, y que ninguna comanda se pierda ni salga dos veces. Hoy el POS y el puente funcionan, pero:

- Si el puente se cae, nadie lo sabe hasta que el cajero intenta imprimir.
- Si una impresora de red se apaga un momento, la comanda se pierde.
- Si falla una de varias impresoras, se reimprime todo.
- TI no tiene una vista central de las cajas.

## Decisiones de Fabián

- `main` del repo `DIMABE-LTDA/dimabe-print-bridge` queda protegido: solo se publica por PR aprobado. Claude trabaja en ramas y deja el PR.
- La actualización del puente puede ser automática de madrugada o solo aviso, configurable por caja. Por defecto: automática y además avisa.
- Las alertas van al tablero «Estado de cajas» de Odoo y por correo a las direcciones de TI configuradas en Odoo.

## Objetivos (los 14 puntos)

### POS / Odoo (`dimabe_pos`)

- **O1. `job_id`.** Cada trabajo de impresión del POS lleva un `job_id` único. Los reintentos automáticos (los del cliente y los de la cola del POS) reutilizan el mismo id, así que el puente no imprime dos veces.
- **O2. Un solo camino de impresión de comandas.**
  - Reemplaza las tres copias actuales: pantalla de mesa, menú de operaciones y envío parcial/vista previa.
  - Rutea por impresora, manda ESC/POS directo cuando la impresora lo soporta y registra el resultado por impresora.
  - Nunca reenvía a una impresora que ya imprimió. Las fallidas pasan a la cola del POS (O4).
  - El envío completo siempre deja la comanda «enviada» (el snapshot se actualiza), aunque la impresión quede pendiente. Esto resuelve I1, I2 e I4 de la auditoría.
- **O3. Indicador del puente.**
  - Ícono flotante en el POS, visible en todas las pantallas operativas: verde, ámbar, rojo o morado (modo prueba).
  - Consulta `GET /api/status` del puente cada 30 s.
  - Al tocarlo se abre un diálogo con el estado, la versión, las impresoras con problemas, la cola del puente y la cola del POS.
- **O4. Cola del POS.**
  - Cuando el puente no responde (o responde error) para una comanda, el trabajo queda en una cola local del POS: `localStorage` por caja, que sobrevive a recargar la página.
  - Reintento automático cada 15 s con el mismo `job_id`.
  - Se ve el contador en el indicador y en la mesa: aviso «comanda sin imprimir» en la pantalla de mesa del pedido.
  - Desde el diálogo se puede reintentar, imprimir en el navegador o descartar con confirmación.
- **O5. Plan B.** Si un trabajo lleva 3 intentos fallidos o el puente lleva más de 60 s sin responder:
  - Aviso persistente con el botón «Imprimir aquí» (impresión por el navegador del mismo ticket).
  - Si el POS tiene pantalla de cocina, el aviso lo dice: la comanda ya llegó a la pantalla.
- **O6. Tablero central.**
  - El POS informa el estado del puente a Odoo cada 60 s mientras la caja está abierta (`pos.config.dimabe_bridge_heartbeat`): versión, modo prueba, impresoras, cola del puente, cola del POS y si hay versión nueva.
  - `pos.config` guarda el último latido. «Estado de cajas» muestra «Puente», «Visto hace», «Versión» y «Pendientes».
  - Un cron cada 5 min detecta y alerta por correo (parámetro `dimabe_pos.bridge_alert_emails`, separado por comas) y en el chatter del POS. Cada problema se avisa como máximo una vez por hora:
    - Caja con sesión abierta sin latido por más de 10 min.
    - Puente caído por más de 5 min.
    - Impresora con error o cola con pendientes por más de 10 min.
    - Versión desactualizada: solo se muestra, no se envía correo.
- **O7. Validaciones al configurar.**
  - Restricción: dos impresoras `dimabe_bridge` del mismo POS no pueden tener la misma IP y puerto.
  - Aviso en la configuración del POS: categorías de productos disponibles en el POS que ninguna impresora cubre, cuando no hay impresora general sin categorías.
  - Botón «Probar» en la ficha de `pos.printer`, reutilizando el widget `bridge_printer_field` que ya tiene la configuración del POS.
  - Campo `dimabe_backup_printer_id` en `pos.printer` (ver B11).

### Puente (`dimabe-print-bridge`)

- **B8. Servicio de Windows más bandeja.**
  - El mismo `.exe` funciona en tres modos: `--service` (servicio `DimabePrintBridge`, LocalSystem, arranque automático, se reinicia solo si falla), `--tray` (solo el ícono, por usuario, al iniciar sesión, consume la API local) y sin argumentos (todo en uno, para desarrollo).
  - El servicio escribe un token local en `ProgramData\DimabePrintBridge\local-token`, que es el token CSRF del panel. La bandeja lo usa para sus acciones.
  - El instalador registra el servicio y la bandeja, y quita la tarea programada de la versión 1.0.
- **B9. Vigilante.** Un hilo consulta `127.0.0.1:<puerto>/api/health` cada 30 s con 5 s de espera. Tras 3 fallas seguidas registra el problema y sale con código 1: el servicio lo reinicia Windows y el modo todo en uno se reinicia solo.
- **B10. Cola persistente.**
  - Los trabajos `kitchen` o `preparation` cuya impresora no responde, no tiene papel o está fuera de línea, y no tienen respaldo, se guardan en `cola.sqlite` con escritura síncrona.
  - El POS recibe `result:true`, `status:"queued"`, `queued:true` y un mensaje claro.
  - Un hilo reintenta por impresora con espera creciente (10 s hasta 60 s) durante 2 h. Después el trabajo vence y genera alerta.
  - Tope de 200 trabajos por impresora.
  - Los duplicados se evitan con `job_id`.
  - Las boletas no se encolan: fallan de inmediato, como hoy.
  - El panel muestra la cola con «reintentar ahora» y «descartar».
- **B11. Impresora de respaldo.** El POS puede mandar `fallback_target` (`printer_name`, o `ip_address` y `ip_port`), sacado de `pos.printer.dimabe_backup_printer_id`. Si la principal no responde, el puente imprime en la de respaldo con una cabecera «RESPALDO DE <principal>» y lo registra. Si el respaldo también falla, encola (B10).
- **B12. Estado real de la impresora de red.**
  - Antes de mandar un trabajo ESC/POS, que no sea etiqueta, a una impresora de red, el puente pide `DLE EOT 2` y `DLE EOT 4` con 500 ms de espera.
  - Sin papel o con la tapa abierta da `BridgeError` con `code="printer_no_paper"` o `"printer_cover_open"`, lo que activa respaldo o cola.
  - Si la impresora no contesta, se envía igual y se recuerda por 1 h que esa impresora no informa su estado.
- **B13. Actualización.**
  - Cada 6 h el puente consulta el último Release.
  - Ajuste por caja `update_mode`: `auto` (por defecto), `notify` u `off`.
  - En `auto`, instala entre 03:00 y 05:00 si no hubo impresiones en los últimos 30 min. Antes:
    - Verifica que el instalador esté firmado por el certificado de huella fija, incluida en el build.
    - Verifica el SHA-256 del asset, si GitHub lo informa.
  - Ejecuta el instalador en modo silencioso.
  - Un script de verificación, programado por el actualizador, comprueba 5 min después que el puente responde con la versión nueva. Si no, reinstala la anterior, guardada en `ProgramData\DimabePrintBridge\updates`.
  - El aviso de versión nueva sale en el panel, la bandeja, `/api/status` y el tablero de Odoo.
- **B14. Alertas.** El puente no manda correos. Su estado llega a Odoo por el latido del POS (O6), y la bandeja muestra notificaciones de Windows ante errores repetidos o cola acumulada.

### API nueva o ampliada del puente

- `GET /api/status` (mismas reglas de origen y clave que `/api/*`) responde con `version`, `test_mode`, `update` (`{available, latest, mode}`), `queue` (`{total, by_target}`), `printers` (estado del monitor) y `recent_errors`.
- `POST /api/print` acepta además `job_id`, `fallback_target` y `queue_on_failure` (por defecto true para `kitchen`/`preparation`). La respuesta puede traer `queued`, `fallback_used` y `code`.

## No objetivos

- Ninguna caja depende de internet para imprimir (solo la actualización lo usa).
- No se cambia el formato de la boleta electrónica ni su flujo DTE.
- No hay correo ni SMTP desde el puente.

## Anti-objetivos

- Imprimir dos veces una comanda.
- Una boleta que sale tarde (por eso no se encola).
- Una actualización que deja la caja sin puente.
- Alertas en cascada (una por problema por hora).

## Restricciones

- Odoo 19, solo `_inherit`. Vistas `<list>`. Rama `feat/pos-impresion-resiliente` desde `feat/print-bridge-descarga-odoo`. `dimabe_pos` 19.0.7.36.0.
- Puente: rama `feat/resiliencia`, PR a `main`. La versión base sube a 1.1.0.
- Pruebas: `pytest` en el puente; `TransactionCase`/`HttpCase` en Odoo para el latido, las alertas, las validaciones y la descarga. La lógica JS nueva del POS va en módulos puros (`utils/printing/*.js`) cubiertos por un tour o por la prueba E2E de Playwright contra el ambiente local.

## Queda a criterio del constructor

Nombres internos, la forma exacta del diálogo y del indicador (siguiendo el estilo `dimabe-ui`), y los textos menores.

## Decisiones tras la revisión del diseño (de Claude)

1. **Latido:** RPC `pos.config.dimabe_bridge_heartbeat(config_id, data)` con usuario del POS. Verifica el grupo POS, que la caja pertenezca a una compañía del usuario y que tenga una sesión abierta. Escribe con `sudo()` solo los campos de salud.
2. **Cola del puente vs. cola del POS:**
   - `queued` significa que la entrega pasa a ser responsabilidad del puente.
   - `/api/status` informa `queue.total`, `queue.oldest_age_s` y `queue.expired` (vencidos de las últimas 24 h). El latido lo lleva a Odoo, que alerta.
   - No hay consulta por `job_id`.
3. **`job_id`:**
   - Uno por (ticket, impresora), creado una vez al armar el trabajo: `crypto.randomUUID()`.
   - Se guarda en la cola del POS, así que es estable entre reintentos y recargas.
   - Para comandas el POS manda además `fallback_target`.
4. **Recuerdo de impresos:** el puente guarda los `job_id` impresos en `cola.sqlite` (tabla `done`) durante 24 h. Sobreviven a un reinicio. Reemplaza la ventana de 5 min en memoria.
5. **«Pendientes»** = cola del puente + cola del POS. La alerta usa la antigüedad del trabajo más viejo (más de 10 min). El latido envía `bridge_queue`, `bridge_oldest_age_s`, `pos_queue`, `pos_oldest_age_s` y `expired`.
6. **Servicio:**
   - Los datos ya viven en `ProgramData` desde la 1.0, así que no hay migración.
   - LocalSystem ve las impresoras locales de la máquina (USB y Windows). No ve conexiones de impresora por usuario (`\servidor\cola`); queda documentado.
   - Las impresoras de red van por IP directa.
7. **Certificado:** el autofirmado `CN=Dimabe Ltda` que ya existe. Huella fija en el build y firma en CI con los secretos.
8. **Indicador:** componente raíz del POS (como `dimabe_global_exit`). Oculto en el login y en el ingreso de empleado.
9. **«Probar» en `pos.printer`:** es una acción del navegador de quien edita (widget `bridge_printer_field`). La ayuda indica usarlo desde el navegador de la caja.
10. **Versión desactualizada:** el latido lleva `update.available` y `update.latest` que informa el puente. Odoo no compara versiones por su cuenta.
