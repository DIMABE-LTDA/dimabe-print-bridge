# Manual de TI · Dimabe Print Bridge

## 1. Instalar en una caja

1. En la caja, abre el POS de Odoo › menú de operaciones › **Diagnóstico de
   impresión** › **Descargar Print Bridge**. También está en Odoo › Punto de
   venta › Configuración › (el POS) › «Puente de impresión Dimabe» ›
   *Descargar Print Bridge*.
2. Ejecuta el instalador **como administrador**. Si Windows muestra «Windows
   protegió su PC», elige *Más información › Ejecutar de todas formas* (pasa con
   cualquier instalador nuevo descargado).
3. En el asistente, deja marcadas:
   - *Iniciar al encender sesión en Windows*.
   - *Detener y reemplazar el puente anterior* (aparece si estaba el script de
     PowerShell).
   - *Confiar en el certificado de Dimabe Ltda* (desde ahí Windows muestra a
     Dimabe Ltda como editor verificado y deja de advertir en las actualizaciones).
4. Puerto: `3210` (debe coincidir con la «URL del puente» de Odoo). Clave API:
   la misma configurada en el POS, si se usa.
5. Al terminar aparece el ícono verde junto al reloj.

Para actualizar: descarga e instala encima; la configuración y la bitácora se
conservan.

## 2. El ícono de la bandeja

| Color | Significa |
|---|---|
| Verde | En línea |
| Ámbar | Alguna impresora tuvo errores en los últimos 30 minutos |
| Morado | Modo prueba: nada sale en papel |
| Gris | No pudo abrir el puerto 3210 (otro programa lo usa; avisa cuál) |

Menú: abrir panel, imprimir ticket de prueba, modo prueba, carpeta de bitácora,
reiniciar, salir.

## 3. El panel (`http://127.0.0.1:3210/panel`)

- **Estado**: si el puente atiende al POS, trabajos de hoy, impresoras con su
  estado, trabajos por hora y lo que requiere atención.
- **Impresoras**: las de Windows y las de red que usó el POS. Botones para probar
  estado, texto, conexión y cajón. Permite probar una IP que no aparece.
- **Bitácora**: un registro por petición. Filtra por día, resultado, impresora y
  documento; al elegir uno se ven los bytes enviados, la vista del ticket y el
  botón para **reenviar** (solo si cocina confirma que no salió). Exporta CSV.
- **Modo prueba**: ver abajo.
- **Configuración**: puerto, ancho de papel, clave API, sitios permitidos, puertos
  de red, panel desde la red y días de bitácora.

## 4. Modo prueba (sin impresoras)

Al encenderlo (panel o bandeja), todo lo que manda el POS se guarda en vez de
imprimirse y el cajón no se abre. En **Modo prueba** se ve cada trabajo como
habría salido (ESC/POS dibujado, ZPL como texto, imagen del recibo). Se puede
simular *sin papel*, *desconectada*, *tiempo de espera* o *falla 1 de cada 3*, con
demora, para ver cómo reacciona el POS. «Prueba sin POS» manda una boleta, una
comanda o una etiqueta de ejemplo. **Apágalo antes de dejar la caja operando.**

## 5. Panel desde otra PC

Configuración › Acceso al panel: define usuario y clave (8+ caracteres), las redes
que pueden entrar (por ejemplo `192.168.1.0/24`) y enciende «Abrir panel a la red».
Se entra por `http://<IP de la caja>:3211/panel`. Desde la red solo se ve y se
prueba: el POS sigue imprimiendo solo desde la misma caja. Si el panel avisa que
falta permiso de firewall, ejecuta como administrador el comando que muestra.
Tras 5 claves incorrectas, esa IP queda bloqueada 10 minutos.

## 6. Problemas frecuentes

| Síntoma | Qué revisar |
|---|---|
| El POS dice que no hay conexión con el puente | ¿Ícono en la bandeja? ¿Gris? Mira qué programa usa el 3210 (el ícono lo avisa) y ciérralo. |
| Panel: «Origin not allowed» en Bitácora › Rechazados | El POS se abre desde un dominio no permitido: agrégalo en Configuración › Sitios permitidos. |
| Impresora de red «No responde» | Encendida, cable, IP igual a la de Odoo (hoja de configuración con FEED al encender). |
| USB «Pausada / Sin conexión en Windows» | Configuración de Windows › Impresoras › reanudar o poner en línea. |
| Comanda impresa dos veces | Bitácora: si figura «Repetido», el puente evitó el duplicado; si no, revisar reintentos en el POS. |
| USB «No existe en Windows» | Ese equipo no tiene una impresora con el nombre exacto de Odoo. Ver «Impresora conectada a otro equipo». |

Logs de texto: `C:\ProgramData\DimabePrintBridge\logs\bridge.log`.

### Impresora conectada a otro equipo

El POS imprime por el puente del equipo donde se usa (`127.0.0.1`), y ese puente busca
la impresora en su propio Windows. Para imprimir desde otro equipo en una impresora USB
compartida, ese equipo necesita una impresora local con el nombre exacto de Odoo y un
puerto `Local Port` que apunte a `\\EQUIPO\COMPARTIDA`, con el mismo driver. Los pasos y
los casos frecuentes están en el panel, en Impresoras › «La impresora está conectada a
otro equipo».

No uses la conexión normal a una impresora compartida: queda con el nombre
`\\EQUIPO\COMPARTIDA` y pertenece al usuario de Windows. El puente corre como servicio con
la cuenta del sistema, así que tampoco usa las credenciales guardadas del usuario. Por eso
la prueba válida es «Probar texto» en el panel, no la página de prueba de Windows, y hay
que confirmar que el ticket salga en papel: Windows puede aceptar el trabajo sin entregarlo.

Si un trabajo lleva más de 2 minutos en la cola o Windows lo marca con error, el panel
muestra «Trabajo atascado en Windows». Lo revisa cada minuto, solo en las impresoras que
usó el POS, y no bloquea los trabajos siguientes.

## 7. Lista de chequeo con hardware real

Antes de dar por buena una caja (o una versión nueva), en una caja con BIXOLON y Zebra:

- [ ] Instalador desde Odoo, con certificado: sin advertencia de editor desconocido.
- [ ] Boleta por USB (BIXOLON): sale completa, sin cortar el borde derecho, nítida.
- [ ] Comanda por red: sale en la impresora de su categoría.
- [ ] Cajón abre al cobrar en efectivo.
- [ ] Etiqueta ZPL (Zebra) desde inventario.
- [ ] Impresora de red apagada: el POS muestra error claro y la bitácora lo registra; al encenderla, reintentar imprime una sola vez.
- [ ] Cola de Windows pausada: el POS recibe «está pausada en Windows», no un falso éxito.
- [ ] Modo prueba: nada sale en papel; al apagarlo vuelve a imprimir.
- [ ] Reiniciar Windows: el puente arranca solo al iniciar sesión.
- [ ] Panel desde otra PC con usuario y clave.

## 8. Novedades de la versión 1.1 (resiliencia)

- **Servicio de Windows.** El puente corre como servicio «Dimabe Print Bridge»: arranca con el equipo aunque nadie inicie sesión, y Windows lo levanta de nuevo si se cae. El ícono de la bandeja es aparte: si lo cierras, el puente sigue funcionando. Un vigilante interno reinicia el servicio si deja de responder.
- **Cola de comandas.** Si una impresora de cocina no responde, no tiene papel o tiene la tapa abierta, la comanda queda en cola y se imprime sola cuando vuelve. El POS recibe «en cola», sin error, y el puente no la imprime dos veces. Se ve en Estado › *Cola de impresión*, con «Reintentar ahora» y «Descartar». A las 2 h sin imprimir vence y se avisa. Las boletas no se encolan.
- **Impresora de respaldo.** Si en Odoo la impresora tiene un respaldo configurado, la comanda sale ahí con la cabecera «RESPALDO DE …».
- **Estado real.** Antes de mandar una comanda a una impresora de red, el puente le pregunta si tiene papel y la tapa cerrada (ESC/POS `DLE EOT`).
- **Actualización.** En Configuración › Actualización: *Instalar sola de madrugada* (por defecto, entre 03:00 y 05:00, sin impresiones en 30 min), *Solo avisar* o *No revisar*. Solo instala instaladores firmados por el certificado de Dimabe Ltda. Si la versión nueva no responde a los 5 minutos, vuelve sola a la anterior.
- **Para el POS.** `GET /api/status` informa versión, cola, impresoras y si hay actualización. El POS lo usa para su indicador y para informar a Odoo («Estado de cajas»).
