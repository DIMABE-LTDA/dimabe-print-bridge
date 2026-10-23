# Verificación posterior a una actualización automática (la programa el actualizador).
# Si 5 minutos después de instalar la versión nueva el puente no responde con esa
# versión, reinstala la anterior. Corre como SYSTEM y se borra a sí misma.
param(
    [Parameter(Mandatory = $true)][string]$Expected,
    [Parameter(Mandatory = $true)][string]$Previous,
    [int]$Port = 3210
)
$ErrorActionPreference = "Continue"
$log = Join-Path $env:ProgramData "DimabePrintBridge\logs\rollback-check.log"
function Log([string]$m) { Add-Content -Path $log -Value "$(Get-Date -Format s) $m" -Encoding UTF8 }

$ok = $false
foreach ($i in 1..4) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
        if ($health.version -eq $Expected) { $ok = $true; break }
        Log "Responde la versión $($health.version), se esperaba $Expected (intento $i)."
    } catch {
        Log "Sin respuesta del puente (intento $i): $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 30
}

if ($ok) {
    Log "Actualización a $Expected verificada."
} elseif (Test-Path $Previous) {
    Log "La versión $Expected no arrancó: se reinstala la anterior."
    Start-Process -FilePath $Previous -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1" -Wait
} else {
    Log "La versión $Expected no arrancó y no hay instalador anterior guardado."
}
schtasks /Delete /F /TN DimabePrintBridgeRollbackCheck | Out-Null
