# Verificación posterior a una actualización automática (la programa el actualizador).
# Vive en la carpeta de instalación (Program Files), fuera del alcance de los usuarios.
# Si 5 minutos después de instalar la versión nueva el puente no responde con esa
# versión, reinstala la anterior, solo si está firmada por el certificado de Dimabe.
# Corre como SYSTEM y se borra a sí misma de las tareas programadas.
param(
    [Parameter(Mandatory = $true)][string]$Expected,
    [Parameter(Mandatory = $true)][string]$Previous,
    [Parameter(Mandatory = $true)][string]$Thumbprint,
    [string]$DataDir = (Join-Path $env:ProgramData "DimabePrintBridge")
)
$ErrorActionPreference = "Continue"
$log = Join-Path $DataDir "logs\rollback-check.log"
function Log([string]$m) { Add-Content -Path $log -Value "$(Get-Date -Format s) $m" -Encoding UTF8 }

# Puerto y clave vigentes (la clave API también protege /api/health).
$port = 3210
$headers = @{}
try {
    $settings = Get-Content (Join-Path $DataDir "settings.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.port) { $port = [int]$settings.port }
    if ($settings.api_key) { $headers["X-Dimabe-Bridge-Key"] = [string]$settings.api_key }
} catch {
    Log "No se pudo leer settings.json: $($_.Exception.Message)"
}

$ok = $false
foreach ($i in 1..4) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -Headers $headers -TimeoutSec 10
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
    $signature = Get-AuthenticodeSignature -LiteralPath $Previous
    $signer = [string]$signature.SignerCertificate.Thumbprint
    $status = [string]$signature.Status
    if ($signer -ne $Thumbprint -or $status -in @("HashMismatch", "NotSigned", "Incompatible", "NotSupportedFileFormat")) {
        Log "La versión $Expected no arrancó, pero el instalador anterior no tiene la firma de Dimabe ($signer, $status): no se ejecuta."
    } else {
        Log "La versión $Expected no arrancó: se reinstala la anterior."
        Start-Process -FilePath $Previous -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
    }
} else {
    Log "La versión $Expected no arrancó y no hay instalador anterior guardado."
}
schtasks /Delete /F /TN DimabePrintBridgeRollbackCheck | Out-Null
