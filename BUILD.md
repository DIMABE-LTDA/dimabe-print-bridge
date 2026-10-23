# Compilar, firmar y publicar

## Automático (lo normal)

Cada push a `main` corre `.github/workflows/release.yml` en un runner Windows:

1. Pruebas (`pytest`).
2. Versión del build: la de `bridge/__init__.py` más el número de ejecución
   (`1.0.0` → `1.0.0.<n>`).
3. Importa el certificado de firma desde los secretos `PFX_BASE64` y `PFX_PASSWORD`.
4. `installer\build.ps1 -SinPruebas`: PyInstaller → firma → Inno Setup → firma.
5. Publica el Release `v<versión>` con `DimabePrintBridge-Setup-<versión>.exe` y
   `dimabe-print-bridge.cer`, marcado como último.

Odoo (`dimabe_pos` ≥ 19.0.7.35.0) toma siempre el último Release. Para un cambio
de versión «grande», sube `__version__` en `bridge/__init__.py`.

## Manual (en un equipo de desarrollo)

Requisitos: Python 3.12, Inno Setup 6 (`winget install --id JRSoftware.InnoSetup -e`).

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File installer\build.ps1
```

Deja `dist\DimabePrintBridge.exe` y `release\DimabePrintBridge-Setup-<versión>.exe`.

## Certificado de firma

Autofirmado `CN=Dimabe Ltda` (RSA 3072, vence a los 5 años). Se crea con
`installer\crear-certificado.ps1`; queda en `Cert:\CurrentUser\My`, el `.cer`
público en `installer\` y el `.pfx` con su clave en
`Documents\Desarrollo\Trabajo\certificados\` (fuera del repo).

Para renovarlo o rotarlo:

```powershell
pwsh -File installer\crear-certificado.ps1 -GenerarClave
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:USERPROFILE\Documents\Desarrollo\Trabajo\certificados\dimabe-print-bridge.pfx"))
$b64 | gh secret set PFX_BASE64 --repo DIMABE-LTDA/dimabe-print-bridge
Get-Content "$env:USERPROFILE\Documents\Desarrollo\Trabajo\certificados\dimabe-print-bridge.pfx.clave.txt" -Raw | gh secret set PFX_PASSWORD --repo DIMABE-LTDA/dimabe-print-bridge
```

Un certificado propio firma, pero Windows solo lo muestra como editor verificado
en equipos que confían en él (`instalar-certificado.ps1`, que el instalador
ejecuta si se deja marcada la opción). Con un certificado comercial basta cambiar
los secretos.
