# Dimabe Print Bridge

Puente local entre el **POS de Odoo** (`dimabe_pos`) y las impresoras de la caja.
El navegador del POS le habla a `http://127.0.0.1:3210` y el puente imprime por
USB (cola de Windows) o por red (TCP 9100). Corre en segundo plano con un ícono
en la bandeja y trae un **panel para TI** con estado, impresoras, bitácora diaria
y modo prueba.

Reemplaza al script `tools/dimabe_print_bridge.ps1` de `grupo_los_lirios` con la
misma API: el POS no necesita cambios.

## Descargar

Cada push a `main` publica un Release firmado. TI lo baja desde el POS de Odoo
(**Diagnóstico de impresión › Descargar Print Bridge**) o desde
[Releases](https://github.com/DIMABE-LTDA/dimabe-print-bridge/releases/latest).
Ver [MANUAL_TI.md](MANUAL_TI.md).

## API (compatible con el POS)

| Ruta | Uso |
|---|---|
| `GET /api/health` | Estado, `version` y `test_mode` |
| `GET /api/printers` | Impresoras de Windows (más las virtuales en modo prueba) |
| `POST /api/print` | `printer_name` **o** `ip_address`+`ip_port`; contenido `raw_base64`, `raw_hex` o `receipt` (imagen, solo USB) |
| `POST /api/cashbox` | Pulso del cajón `ESC p 0 25 250` |

Seguridad: solo atiende a sitios permitidos (`*.odoo.com`, `*.grupoloslirios.cl`,
localhost), clave API opcional en `X-Dimabe-Bridge-Key`, e impresoras de red solo
con IPv4 privada y puertos 9100-9109. Cuerpo máximo 20 MB. Un `job_id` repetido
dentro de 5 minutos no se vuelve a imprimir.

## Panel para TI

`http://127.0.0.1:3210/panel` en la misma caja (sin clave). Opcionalmente desde
la red en el puerto 3211 con usuario y clave. Capturas en `docs/capturas/`.

## Datos

`C:\ProgramData\DimabePrintBridge\`: `settings.json`, `bitacora.sqlite` (un registro
por petición, se borra a los N días configurados) y `logs\bridge.log`.
`DIMABE_BRIDGE_DATA_DIR` lo cambia (pruebas y desarrollo).

## Desarrollo

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python main.py --port 3299 --no-tray --demo   # datos ficticios del día
```

| Módulo | Responsabilidad |
|---|---|
| `bridge/config.py` | Ajustes persistentes y rutas de datos |
| `bridge/security.py` | Orígenes, destinos de red, clave API, contraseñas, bloqueo de login |
| `bridge/journal.py` | Bitácora SQLite (cola + hilo escritor, consultas, purga, CSV) |
| `bridge/escpos.py` | Intérprete ESC/POS para la vista previa |
| `bridge/backends.py` | Windows (RAW, imagen, red) |
| `bridge/capture.py` | Modo prueba: impresoras virtuales y simulación de fallas |
| `bridge/service.py` | Destino, duplicados, un trabajo a la vez por impresora |
| `bridge/api.py` | API compatible |
| `bridge/panel_api.py`, `panel_views.py` | Panel y su autenticación |
| `bridge/monitor.py`, `server.py`, `tray.py` | Estado de impresoras, servidores, bandeja |

Compilar y firmar: [BUILD.md](BUILD.md).
