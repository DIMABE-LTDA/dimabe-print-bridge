# Registra, actualiza o quita el servicio de Windows "DimabePrintBridge".
# Lo llama el instalador (como administrador):
#   servicio.ps1 -Accion instalar -Exe "C:\Program Files\DimabePrintBridge\DimabePrintBridge.exe"
#   servicio.ps1 -Accion detener
#   servicio.ps1 -Accion quitar
param(
    [Parameter(Mandatory = $true)][ValidateSet("instalar", "detener", "quitar")][string]$Accion,
    [string]$Exe = ""
)
$ErrorActionPreference = "Continue"
$name = "DimabePrintBridge"
$existing = Get-Service -Name $name -ErrorAction SilentlyContinue

function Stop-Bridge {
    if ($existing -and $existing.Status -ne "Stopped") {
        sc.exe stop $name | Out-Null
        foreach ($i in 1..20) {
            if ((Get-Service -Name $name).Status -eq "Stopped") { break }
            Start-Sleep -Milliseconds 500
        }
    }
    # Procesos de la versión 1.0 (todo en uno) y del núcleo que pudieran quedar.
    Get-Process -Name "DimabePrintBridge" -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -eq 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
}

switch ($Accion) {
    "detener" { Stop-Bridge }
    "instalar" {
        Stop-Bridge
        # La 1.0 arrancaba con una tarea programada al iniciar sesión: la reemplaza el servicio.
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        # Los instaladores descargados se ejecutan como SYSTEM: solo SYSTEM y administradores
        # pueden escribir en esa carpeta (un usuario de la caja no puede cambiarlos).
        $updates = Join-Path $env:ProgramData "DimabePrintBridge\updates"
        New-Item -ItemType Directory -Force -Path $updates | Out-Null
        icacls.exe $updates /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
        $bin = "`"$Exe`" --service"
        if ($existing) {
            sc.exe config $name binPath= $bin start= delayed-auto DisplayName= "Dimabe Print Bridge" | Out-Null
        } else {
            sc.exe create $name binPath= $bin start= delayed-auto DisplayName= "Dimabe Print Bridge" | Out-Null
        }
        sc.exe description $name "Puente local entre el POS de Odoo y las impresoras de esta caja." | Out-Null
        # Si el proceso cae, Windows lo levanta de nuevo (5 s, 5 s, 30 s; contador diario).
        sc.exe failure $name reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
        sc.exe failureflag $name 1 | Out-Null
        sc.exe start $name | Out-Null
    }
    "quitar" {
        Stop-Bridge
        sc.exe delete $name | Out-Null
    }
}
