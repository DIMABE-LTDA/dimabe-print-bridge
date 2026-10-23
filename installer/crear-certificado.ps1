# Crea el certificado autofirmado de firma de código "CN=Dimabe Ltda".
#
# - Queda en el almacén del usuario (Cert:\CurrentUser\My), que es de donde
#   build.ps1 lo toma para firmar.
# - Exporta el .cer público a installer\dimabe-print-bridge.cer (va en el
#   instalador y en Odoo) y el .pfx con su clave fuera del repositorio.
#
# Uso:  pwsh -File installer\crear-certificado.ps1
#       (clave del .pfx: se pide; o DIMABE_PFX_PASSWORD; o -GenerarClave)
param(
    [string]$Salida = "$env:USERPROFILE\Documents\Desarrollo\Trabajo\certificados",
    [switch]$GenerarClave
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$existente = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq "CN=Dimabe Ltda" -and $_.NotAfter -gt (Get-Date).AddDays(30) } |
    Sort-Object NotAfter -Descending | Select-Object -First 1
if ($existente) {
    Write-Host "Ya existe un certificado vigente: $($existente.Thumbprint) (vence $($existente.NotAfter.ToString('yyyy-MM-dd')))"
    $cert = $existente
} else {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=Dimabe Ltda" `
        -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 -KeyExportPolicy Exportable `
        -NotAfter (Get-Date).AddYears(5) -CertStoreLocation Cert:\CurrentUser\My
    Write-Host "Certificado creado: $($cert.Thumbprint)"
}

New-Item -ItemType Directory -Force -Path $Salida | Out-Null
Export-Certificate -Cert $cert -FilePath (Join-Path $root "installer\dimabe-print-bridge.cer") -Type CERT | Out-Null
Set-Content -Path (Join-Path $root "installer\cert-thumbprint.txt") -Value $cert.Thumbprint -NoNewline

$pfx = Join-Path $Salida "dimabe-print-bridge.pfx"
if (-not (Test-Path $pfx)) {
    if ($GenerarClave) {
        $plain = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
        $clave = ConvertTo-SecureString $plain -AsPlainText -Force
        $archivoClave = "$pfx.clave.txt"
        Set-Content -Path $archivoClave -Value $plain -NoNewline
        # Solo el usuario actual puede leer la clave.
        icacls $archivoClave /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
        Write-Host "Clave del .pfx guardada en $archivoClave"
    } elseif ($env:DIMABE_PFX_PASSWORD) {
        $clave = ConvertTo-SecureString $env:DIMABE_PFX_PASSWORD -AsPlainText -Force
    } else {
        $clave = Read-Host -AsSecureString "Clave para proteger el .pfx"
    }
    Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $clave | Out-Null
    Write-Host "Respaldo .pfx: $pfx"
}
Write-Host "Huella: $($cert.Thumbprint)"
