# Instala el certificado de Dimabe como editor de confianza en este equipo.
# Así Windows muestra "Dimabe Ltda" como editor verificado del instalador y
# del programa. Ejecutar como administrador:
#   powershell -ExecutionPolicy Bypass -File instalar-certificado.ps1
param([string]$Certificado = (Join-Path $PSScriptRoot "dimabe-print-bridge.cer"))
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Ejecuta este script como administrador (clic derecho › Ejecutar como administrador)."
}
if (-not (Test-Path $Certificado)) { throw "No se encontró $Certificado" }

Import-Certificate -FilePath $Certificado -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
Import-Certificate -FilePath $Certificado -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
Write-Host "Certificado de Dimabe Ltda instalado como editor de confianza." -ForegroundColor Green
