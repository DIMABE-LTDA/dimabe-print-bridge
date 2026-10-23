# Compila Dimabe Print Bridge: pruebas → .exe (PyInstaller) → firma → instalador (Inno Setup) → firma.
# Ejecutar desde la raíz del proyecto:
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
param([switch]$SinPruebas)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Falta el entorno virtual: py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt" }

$version = (Select-String -Path "bridge\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "==> Dimabe Print Bridge $version" -ForegroundColor Cyan

if (-not $SinPruebas) {
    Write-Host "==> Pruebas" -ForegroundColor Cyan
    & $py -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Las pruebas fallaron: no se compila." }
}

# Metadatos de versión del .exe (Propiedades › Detalles en Windows).
$parts = ($version.Split(".") + @("0", "0", "0"))[0..3] -join ", "
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($parts), prodvers=($parts)),
  kids=[
    StringFileInfo([StringTable('040A04B0', [
      StringStruct('CompanyName', 'Dimabe Ltda'),
      StringStruct('FileDescription', 'Dimabe Print Bridge'),
      StringStruct('FileVersion', '$version'),
      StringStruct('ProductName', 'Dimabe Print Bridge'),
      StringStruct('ProductVersion', '$version'),
      StringStruct('LegalCopyright', 'Dimabe Ltda')])]),
    VarFileInfo([VarStruct('Translation', [1034, 1200])])
  ]
)
"@ | Set-Content -Path "installer\version_info.txt" -Encoding UTF8

Write-Host "==> Ejecutable (PyInstaller)" -ForegroundColor Cyan
& $py -m PyInstaller DimabePrintBridge.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló." }
$exe = Join-Path $root "dist\DimabePrintBridge.exe"

function Sign-File([string]$Path) {
    $thumb = (Get-Content "installer\cert-thumbprint.txt" -ErrorAction SilentlyContinue)
    $cert = if ($thumb) { Get-Item "Cert:\CurrentUser\My\$($thumb.Trim())" -ErrorAction SilentlyContinue }
    if (-not $cert) {
        Write-Host "!! Sin certificado de firma (corre installer\crear-certificado.ps1): $Path queda sin firmar." -ForegroundColor Yellow
        return
    }
    # Con certificado propio el estado es "UnknownError" hasta que el equipo confía
    # en él (instalar-certificado.ps1); lo que importa es que quede firmado y sellado.
    try {
        Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256 `
            -TimestampServer "http://timestamp.digicert.com" -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "   Sin sello de tiempo (¿sin internet?); se firma sin él." -ForegroundColor Yellow
        Set-AuthenticodeSignature -FilePath $Path -Certificate $cert -HashAlgorithm SHA256 | Out-Null
    }
    $sig = Get-AuthenticodeSignature -FilePath $Path
    if (-not $sig.SignerCertificate) { throw "No se pudo firmar $Path" }
    Write-Host "   Firmado por $($sig.SignerCertificate.Subject), sello de tiempo: $([bool]$sig.TimeStamperCertificate)"
}
Write-Host "==> Firma del ejecutable" -ForegroundColor Cyan
Sign-File $exe

$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "${env:ProgramFiles}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
    if (-not $iscc -and (Test-Path $candidate)) { $iscc = $candidate }
}
if (-not $iscc) { throw "Falta Inno Setup 6: winget install --id JRSoftware.InnoSetup -e" }

Write-Host "==> Instalador (Inno Setup)" -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$version" "installer\DimabePrintBridge.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló." }
$setup = Join-Path $root "release\DimabePrintBridge-Setup-$version.exe"
Write-Host "==> Firma del instalador" -ForegroundColor Cyan
Sign-File $setup
Write-Host "==> Listo: $setup" -ForegroundColor Green
