# Dimabe Print Bridge (app de escritorio): diseño

Fecha: 23-10-2026 · Autor: Fabián (Dimabe) con Claude · Estado: aprobado por maquetas, pendiente de plan

## Intención

Hoy el puente de impresión es un script de PowerShell (`grupo_los_lirios/tools/dimabe_print_bridge.ps1`) que corre en cada caja. Funciona, pero TI no ve qué hace: no hay bitácora, no hay forma de probar sin impresoras y la instalación es manual (tarea programada a mano, `pwsh`, `urlacl`).

Se reemplaza por una aplicación de Windows con la misma forma que `serialport_api` (que Fabián ya usa en producción): un `.exe` que corre en segundo plano con ícono en la bandeja, un panel web local para TI, bitácora diaria en SQLite, un modo de prueba con impresoras virtuales y un instalador. Además, el instalador se descarga desde Odoo, para que TI no dependa de un pendrive.

## Usuarios

- **TI de Dimabe / del cliente**: instala, revisa por qué no imprimió, prueba el POS sin impresoras. Es el único público del panel.
- **El POS (navegador)**: el cliente HTTP. No cambia: misma URL `http://127.0.0.1:3210` y mismos endpoints.
- El cajero no usa el panel.

## Objetivos

1. **Compatibilidad total** con el POS actual (`dimabe_pos` en PRD) y las etiquetas ZPL de `dimabe_base`: mismos endpoints, payloads y respuestas JSON que `tools/dimabe_print_bridge.ps1` (rama `fix/pos-impresion-criticos-23102026`), incluidas las protecciones de seguridad recién agregadas.
2. **Panel para TI** con las 7 pantallas aprobadas en la maqueta (`maquetas/panel-completo.html`, copiada en este repo): Estado, Impresoras, Bitácora del día, Modo prueba, Configuración, Entrada desde la red, Bandeja e instalador.
3. **Bitácora en SQLite**: un registro por petición; se consulta por día; retención configurable (por defecto 90 días).
4. **Modo prueba**: impresoras virtuales que aceptan todo, guardan el trabajo y muestran cómo habría salido el ticket; simulación de fallas y demoras; botones para enviar trabajos de ejemplo.
5. **Panel desde la red** (opcional, apagado por defecto) con usuario y clave.
6. **Instalador** Inno Setup con inicio automático y reemplazo del puente PowerShell anterior.
7. **Descarga desde Odoo**: el instalador vigente se sube a Odoo y se descarga desde el POS.

## No objetivos

- No se cambia el protocolo ni el formato de impresión del POS (ESC/POS, imagen, raster `GS v 0` ya resuelto en el POS).
- No es un servicio de Windows (Session 0): corre en la sesión del usuario, como `serialport_api`, para tener acceso a impresoras USB del usuario y al ícono.
- No soporta Linux ni macOS en esta versión. El script Linux existente sigue disponible aparte.
- No hay actualización automática: el instalador nuevo se baja desde Odoo y se ejecuta encima.
- No se imprimen trabajos desde el panel remoto (solo lectura más pruebas explícitas; ver Seguridad).

## Anti-objetivos (sería un fracaso aunque funcione)

- Que el POS tenga que cambiar su configuración para usar la app nueva.
- Que el panel o la bitácora hagan más lento imprimir (la escritura en SQLite no puede bloquear la respuesta al POS más de unos ms).
- Que la app reabra los huecos de seguridad recién cerrados (cualquier sitio web imprimiendo, SSRF a la red).
- Que el modo prueba quede encendido sin que se note (debe verse en el panel, en el ícono y en la respuesta de `/api/health`).
- Que la bitácora llene el disco.

## Restricciones

- Windows 10/11 x64. Python 3.12 (el que hay en el equipo de build), empaquetado con PyInstaller en un solo `.exe` sin consola.
- Pila decidida (igual que `serialport_api`): Flask + waitress, `pystray` + Pillow para la bandeja, `sqlite3` de la librería estándar. Para imprimir en Windows: `pywin32` (`win32print` para RAW y `win32ui`/`ImageWin` de Pillow para imprimir la imagen del recibo).
- Datos en `%LOCALAPPDATA%\DimabePrintBridge` (`settings.json`, `bitacora.sqlite`, `logs\`).
- Puerto de la API: `3210` en `127.0.0.1` (fijo por defecto, configurable).
- Panel local en el mismo puerto (`/panel`). Panel remoto en un puerto aparte (por defecto `3211`) escuchando en todas las interfaces solo si está habilitado; ese puerto sirve únicamente el panel y su API de lectura, nunca `/api/print` ni `/api/cashbox`.
- Sin dependencias de internet en tiempo de ejecución: el panel no usa CDN; las fuentes (IBM Plex Sans/Mono) e íconos se empaquetan.
- Idioma de la interfaz: español de Chile.

## API compatible (puerto 3210)

| Ruta | Comportamiento |
|---|---|
| `OPTIONS *` | Preflight CORS. 204 sin cuerpo si el `Origin` está permitido; 403 si no. |
| `GET /api/health` | `{result, status, message, listen_url, version, test_mode}` |
| `GET /api/printers` | Impresoras de Windows (`Name, DriverName, PortName, Type, Shared`), más las virtuales si el modo prueba está activo. |
| `POST /api/print` | Destino `printer_name` o `ip_address`+`ip_port`; contenido `raw_base64`, `raw_hex` o `receipt` (imagen base64, solo USB); `paper_width_mm`, `document_type`. |
| `POST /api/cashbox` | Pulso `ESC p 0 25 250` al destino. |

Reglas que se mantienen tal cual del script PowerShell: orígenes permitidos con comodines (por defecto `https://*.odoo.com`, `http://*.localhost[:*]`, `http://localhost[:*]`, `http://127.0.0.1[:*]`); peticiones sin `Origin` aceptadas; API key opcional en `X-Dimabe-Bridge-Key`; IP de red solo IPv4 privada literal (10/8, 172.16/12, 192.168/16, 169.254/16) y puerto en la lista permitida (9100-9109); `ip_address` gana si vienen ambos destinos; imagen hacia IP se rechaza; forma del JSON `{result, status, message}`; códigos actuales (200, 204, 401, 403, 404, 500).

Mejoras permitidas sin romper al cliente:
- Errores de validación del pedido devuelven 400 en vez de 500 (el POS solo mira `result`).
- Límite de cuerpo 20 MB → 413.
- Cada petición atendida en su propio hilo (waitress), con un lock por destino para no mezclar trabajos en la misma impresora.
- Timeouts TCP a impresoras de red: 3 s conexión, 5 s escritura.
- `job_id` opcional en el payload: si llega repetido dentro de 5 minutos, se responde éxito sin volver a imprimir (`duplicate: true`). Hoy el POS no lo envía; queda listo para cuando lo haga.
- Antes de imprimir por USB se consulta el estado de la cola de Windows (pausada / sin conexión / error) y se responde error claro en vez de "éxito" falso.

## Bitácora (SQLite)

Tabla `events`, un registro por petición a la API y por evento del servicio (inicio, parada, cambio de configuración, modo prueba encendido/apagado):

- `id`, `ts` (UTC ISO), `day` (fecha local, indexada), `kind` (`print`, `cashbox`, `health`, `printers`, `rejected`, `service`, `config`), `document_type`, `target_label`, `target_kind` (`usb`/`ip`/`virtual`), `printer_alias` (nombre que le dio el POS si viene, si no el destino), `origin`, `client_ip`, `bytes`, `duration_ms`, `result` (`ok`/`error`/`rejected`/`duplicate`), `http_status`, `message`, `job_id`, `test_mode` (0/1), `payload_kind` (`raw`/`image`/`none`), `payload` (BLOB, opcional).
- Se guardan los bytes RAW si «Guardar bytes enviados» está activo (por defecto sí). Las imágenes de boleta no se guardan por defecto (opción aparte).
- La escritura va a una cola en memoria y un hilo único la vuelca a SQLite (modo WAL). La respuesta al POS no espera la escritura.
- Limpieza diaria (al arrancar y cada 6 h) de registros más antiguos que la retención configurada (1-3650 días, por defecto 90), seguida de `VACUUM` incremental.
- Exportación a CSV por día.
- No se registran `GET /api/health` repetidos del diagnóstico más de una vez por minuto por origen, para no inundar la tabla.

## Modo prueba

- Interruptor en el panel y en el menú de la bandeja. Persistente en `settings.json`. Mientras está activo: banner morado en todas las pantallas, ícono morado, `test_mode: true` en `/api/health` y en cada respuesta de impresión.
- Todo `/api/print` y `/api/cashbox` va a impresoras virtuales: no se toca el spooler ni la red. Las virtuales se nombran con el destino recibido (nombre USB o `ip:puerto`), para ver el ruteo que hizo el POS.
- Cada trabajo se guarda (en la bitácora con `test_mode=1` y payload siempre guardado) y se muestra en el panel:
  - RAW ESC/POS: un intérprete propio convierte los bytes a una vista de ticket (texto con alineación, negrita, doble alto/ancho, cortes, avances, y raster `GS v 0` dibujado como imagen). Comandos desconocidos se muestran como marcas discretas, no rompen la vista.
  - ZPL: se muestra el texto ZPL tal cual (no se renderiza la etiqueta).
  - Imagen: se muestra la imagen.
  - Cajón: se registra «apertura de cajón».
- Simulación configurable: respuesta (`ok`, `sin papel`, `desconectada`, `tiempo de espera`, `falla 1 de cada 3`) y demora (0, 1, 5, 20 s).
- Botones para enviar una boleta, una comanda y una etiqueta ZPL de ejemplo a través del mismo camino HTTP.
- «Enviar a impresora real…» reenvía un trabajo capturado a una impresora real elegida (pide confirmación).

## Panel (UI)

Fuente de verdad visual: `maquetas/panel-completo.html` (aprobada). Mantener su sistema: IBM Plex Sans para interfaz y Plex Mono solo para datos (IP, bytes, rutas); tokens de color de la maqueta con modo claro/oscuro (sigue al sistema, con selector); pills de estado con texto (no solo color); tablas densas con números tabulares; íconos SVG de trazo único.

- Implementación: HTML servido por Flask (plantillas Jinja) + JavaScript sin framework, que consulta una API JSON interna (`/panel/api/...`) cada 5 s en Estado y Bitácora del día (con pausa cuando la pestaña no está visible). Sin build de frontend.
- Pantallas y contenido: las de la maqueta. «Requiere atención» se calcula: impresora con ≥1 error en los últimos 30 minutos, cola de Windows pausada/sin conexión, modo prueba encendido, API key vacía, panel abierto a la red sin clave.
- Estado de impresoras: se refresca en segundo plano cada 60 s (cola de Windows para USB; conexión TCP de 1 s para IP, solo las IP vistas en la bitácora de los últimos 7 días).
- Acciones con efecto en papel (probar texto, abrir cajón, reenviar, enviar a impresora real) piden confirmación y quedan en la bitácora con `origin = panel`.

## Panel desde la red

- Apagado por defecto. Al encenderlo se exige usuario y clave (hash PBKDF2 en `settings.json`) y una lista de redes permitidas en CIDR (por defecto la /24 del equipo).
- Puerto aparte (3211, configurable), `0.0.0.0`. Sirve solo `/panel` y `/panel/api/*` de lectura más las acciones de prueba del panel. No sirve `/api/print` ni `/api/cashbox`.
- Sesión por cookie `HttpOnly`, `SameSite=Strict`, 8 h. Bloqueo de 10 minutos tras 5 intentos fallidos por IP.
- En el equipo local (`127.0.0.1:3210/panel`) no se pide clave.
- Al habilitarlo por primera vez, la app intenta agregar la regla de firewall de Windows para ese puerto (requiere elevación; si no se puede, el panel lo indica con el comando a ejecutar).

## Bandeja

Ícono generado por código (impresora estilizada), color según estado: verde en línea, ámbar si alguna impresora tiene errores recientes, morado en modo prueba, gris si el servidor no pudo levantar el puerto. Menú: título con versión y resumen, Abrir panel, Imprimir ticket de prueba, Modo prueba (encender/apagar), Abrir carpeta de bitácora, Reiniciar servicio, Salir.

Si el puerto 3210 está ocupado (por ejemplo, por el puente PowerShell viejo), la app no se cierra: el ícono queda gris, muestra una notificación de Windows con el nombre y PID del proceso que usa el puerto, y reintenta levantar el puerto cada 10 s. Desde el menú de la bandeja se puede abrir la carpeta de logs para ver el detalle.

## Instalador (Inno Setup)

- `DimabePrintBridge-Setup-<versión>.exe`, en español, instala en `Program Files\DimabePrintBridge`.
- Opciones: inicio automático al iniciar sesión (tarea programada `Dimabe Print Bridge`, igual que `serialport_api`: grupo Usuarios, reintento cada minuto); reemplazar el puente PowerShell anterior (detiene y elimina la tarea programada del script si existe); abrir el panel a la red (no por defecto); clave API opcional; puerto (por defecto 3210). Lo ingresado se escribe en `settings.json` en el primer arranque.
- Al actualizar cierra la instancia en ejecución antes de copiar. Al desinstalar quita la tarea; los datos en `%LOCALAPPDATA%` se conservan salvo que se marque «borrar bitácora».
- Script `installer\build.ps1` que genera el `.exe` (PyInstaller) y el instalador (ISCC).

## Descarga desde Odoo

En `grupo_los_lirios`, módulo `dimabe_pos` (rama nueva desde `origin/PRD`):

- Modelo nuevo `dimabe.print.bridge.release`: `name` (versión, ej. `1.0.0`), `installer` (Binary, `attachment=True`), `installer_filename`, `notes` (Text: cambios), `active`, `is_current` (Boolean, único vigente), `upload_date`. Acceso: lectura para usuarios de POS (`point_of_sale.group_pos_user`), escritura para administradores de POS (`point_of_sale.group_pos_manager`).
- Menú «Punto de venta › Configuración › Print Bridge (instalador)» para subir versiones; al marcar una como vigente se desmarca la anterior.
- Controlador `GET /dimabe_pos/print_bridge/download` (auth `user`, usuario del POS): entrega el instalador vigente como descarga. 404 con mensaje claro si no hay ninguno.
- Botón «Descargar Print Bridge» en:
  - la configuración del POS, en la sección del puente;
  - el popup de diagnóstico de impresión del POS, destacado cuando el puente no responde («El puente no está instalado o no está corriendo en este equipo»).
- El panel de la app muestra su versión, y `/api/health` la publica; el popup del POS compara con la vigente en Odoo y avisa si hay una más nueva.
- Tests: acceso al modelo por grupo, unicidad de vigente, controlador sin vigente → 404, con vigente → archivo.

## Pruebas

- `pytest` de la app, sin impresoras: compatibilidad de API (mismos casos que se probaron sobre el script: orígenes, IP pública, DNS, puerto 22, IP privada, nombre con ñ, payload de 4 MB), bitácora (registro, retención, CSV), modo prueba (captura, simulación de fallas, intérprete ESC/POS con casos de alineación, tamaños, corte y raster), login remoto y bloqueo, y que el puerto remoto no sirva `/api/print`.
- Impresión real de Windows mockeada en tests; verificación manual en la caja de pruebas con la BIXOLON y la Zebra antes de entregar.
- Odoo: tests del módulo como se indicó, sobre una copia restaurada de PRD.

## Entregables

- Repo nuevo `proyectos/dimabe_print_bridge` (git), con README, BUILD.md y MANUAL_TI.md.
- `DimabePrintBridge-Setup-1.0.0.exe`.
- Rama `feat/print-bridge-descarga-odoo` en `grupo_los_lirios` con el modelo, controlador y botones (versión de `dimabe_pos` subida).
- `tools/dimabe_print_bridge.md` actualizado apuntando a la app nueva.

## Decisiones tomadas por Claude (revisables)

- El panel remoto ve y prueba, pero el POS solo puede imprimir desde la misma caja.
- No se guardan imágenes de boleta en la bitácora por defecto.
- Arranque al iniciar sesión, no como servicio de Windows.
- `job_id` para deduplicar queda implementado en la app, pero el POS no lo envía todavía (cambio aparte).
- El instalador se guarda en Odoo como adjunto, no en el repositorio del módulo, porque pesa decenas de MB.

## Decisiones tras la revisión del diseño

- **Datos por equipo, no por usuario:** `settings.json`, `bitacora.sqlite` y `logs\` van en `%PROGRAMDATA%\DimabePrintBridge` (el instalador crea la carpeta y da permiso de modificación al grupo Usuarios). Así lo que se ingresa al instalar, aunque se instale con otra cuenta de administrador, lo ve la cuenta que usa la caja. Reemplaza la ruta `%LOCALAPPDATA%` indicada arriba. Si dos usuarios inician sesión a la vez, la segunda instancia detecta el puerto ocupado por la primera y queda en gris sin pelear por él.
- **Acciones del panel local protegidas:** aunque el panel local no pide clave, toda acción que imprime, abre el cajón o cambia la configuración exige: `Host` igual a `127.0.0.1:<puerto>` o `localhost:<puerto>` (defensa contra DNS rebinding), `Origin` igual al del propio panel, y un token anti-CSRF que el panel recibe al cargar. Las lecturas del panel también validan `Host`.
- **Imagen del recibo por USB:** se dibuja escalada al **área imprimible** que informa el driver (no al ancho nominal), manteniendo la proporción; el ancho nominal `paper_width_mm` solo define el tamaño de página. Se valida visualmente con la impresora real.
- **Rama de Odoo:** `feat/print-bridge-descarga-odoo` desde `origin/PRD`, independiente de `fix/pos-impresion-criticos-23102026`. El popup de diagnóstico y la sección «Puente de impresión Dimabe» ya existen en PRD. Si el fix se sube primero, se resuelve el choque de versión del manifiesto al integrar.

- **Firma de código (pedido de Fabián: «puede crearlo»):** se crea un certificado autofirmado de firma de código `CN=Dimabe Ltda` (RSA 3072, 5 años) en el almacén del usuario del equipo de build. Se exporta el `.pfx` con clave a una carpeta fuera del repo (`%USERPROFILE%\Documents\Desarrollo\Trabajo\certificados\`) y el `.cer` público al repo. `build.ps1` firma el `.exe` y el instalador con `Set-AuthenticodeSignature` y sello de tiempo. Un certificado autofirmado no evita el aviso de SmartScreen en cajas nuevas: para que Windows muestre «Dimabe Ltda» como editor verificado, el `.cer` debe estar en «Editores de confianza» y «Entidades raíz de confianza» del equipo. Se entrega `instalar-certificado.ps1` (el kit lo corre como administrador) y Odoo ofrece el `.cer` junto al instalador. El manual de TI explica ambos pasos. Si a futuro compran un certificado comercial, solo cambia la huella que usa `build.ps1`.
- **Herramienta de build:** se instala Inno Setup con `winget install JRSoftware.InnoSetup` en el equipo de Fabián (autorizado).
- **Prueba con hardware real:** la hace Fabián en una caja con BIXOLON y Zebra, siguiendo una lista de chequeo que se entrega en `MANUAL_TI.md`. La entrega se valida con pruebas automáticas y con el modo prueba.

## Queda a criterio del constructor

- Estructura interna de módulos Python, nombres de archivos y de funciones.
- Detalles del intérprete ESC/POS para comandos poco comunes.
- Textos menores de la interfaz, siguiendo el tono de la maqueta.
