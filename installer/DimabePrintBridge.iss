; Instalador de Dimabe Print Bridge (Inno Setup 6+)
; Lo compila installer\build.ps1 (pasa /DMyAppVersion=x.y.z).

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppName "Dimabe Print Bridge"
#define MyAppPublisher "Dimabe Ltda"
#define MyAppExeName "DimabePrintBridge.exe"
#define MyTaskName "DimabePrintBridge"
#define OldTaskName "Dimabe Print Bridge"

[Setup]
AppId={{6F3C2B1A-4D5E-4F60-9A7B-3E2D1C0B9A87}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
DefaultDirName={autopf}\DimabePrintBridge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=DimabePrintBridge-Setup-{#MyAppVersion}
SetupIconFile=..\bridge\static\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "autostart"; Description: "Iniciar al encender sesión en Windows (sin ventana)"; GroupDescription: "Inicio automático:"
Name: "replaceold"; Description: "Detener y reemplazar el puente anterior (script de PowerShell)"; GroupDescription: "Puente anterior:"; Check: OldBridgeExists
Name: "remotepanel"; Description: "Abrir el panel a la red para TI (se configura la clave en el panel)"; GroupDescription: "Panel:"; Flags: unchecked
Name: "trustcert"; Description: "Confiar en el certificado de Dimabe Ltda en este equipo"; GroupDescription: "Seguridad:"

[Dirs]
Name: "{commonappdata}\DimabePrintBridge"; Permissions: users-modify

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dimabe-print-bridge.cer"; DestDir: "{app}\certificado"; Flags: ignoreversion
Source: "instalar-certificado.ps1"; DestDir: "{app}\certificado"; Flags: ignoreversion

[Icons]
Name: "{group}\Abrir panel del Print Bridge"; Filename: "http://127.0.0.1:{code:GetPort}/panel"
Name: "{group}\Iniciar {#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Unregister-ScheduledTask -TaskName '{#OldTaskName}' -Confirm:$false -ErrorAction SilentlyContinue; Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*dimabe_print_bridge.ps1*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }"""; \
  Flags: runhidden; Tasks: replaceold; StatusMsg: "Deteniendo el puente anterior..."
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\certificado\instalar-certificado.ps1"""; \
  Flags: runhidden; Tasks: trustcert; StatusMsg: "Instalando el certificado de Dimabe Ltda..."
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""$a = New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}' -WorkingDirectory '{app}'; $t = New-ScheduledTaskTrigger -AtLogOn; $p = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Limited; $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew; Register-ScheduledTask -Force -TaskName '{#MyTaskName}' -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null"""; \
  Flags: runhidden; Tasks: autostart; StatusMsg: "Configurando el inicio automático..."
Filename: "netsh.exe"; \
  Parameters: "advfirewall firewall add rule name=""Dimabe Print Bridge panel"" dir=in action=allow protocol=TCP localport=3211"; \
  Flags: runhidden; Tasks: remotepanel; StatusMsg: "Abriendo el puerto del panel en el firewall..."
Filename: "{app}\{#MyAppExeName}"; Description: "Iniciar {#MyAppName} ahora"; Flags: nowait postinstall runasoriginaluser

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/im {#MyAppExeName} /f"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Unregister-ScheduledTask -TaskName '{#MyTaskName}' -Confirm:$false -ErrorAction SilentlyContinue"""; \
  Flags: runhidden; RunOnceId: "DelTask"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Dimabe Print Bridge panel"""; Flags: runhidden; RunOnceId: "DelFirewall"

[Code]
var
  ConfigPage: TInputQueryWizardPage;

function OldBridgeExists(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\schtasks.exe'), '/Query /TN "{#OldTaskName}"', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure InitializeWizard();
begin
  ConfigPage := CreateInputQueryPage(wpSelectTasks, 'Conexión con el POS',
    'Debe coincidir con lo configurado en Odoo.',
    'La URL del puente en Odoo es http://127.0.0.1:<puerto>. La clave API es opcional pero recomendada: debe ser la misma que en la configuración del POS.');
  ConfigPage.Add('Puerto:', False);
  ConfigPage.Add('Clave API (opcional):', False);
  ConfigPage.Values[0] := '3210';
end;

function GetPort(Param: String): String;
begin
  if Assigned(ConfigPage) and (Trim(ConfigPage.Values[0]) <> '') then
    Result := Trim(ConfigPage.Values[0])
  else
    Result := '3210';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
begin
  Result := True;
  if CurPageID = ConfigPage.ID then
  begin
    Port := StrToIntDef(Trim(ConfigPage.Values[0]), 0);
    if (Port < 1) or (Port > 65535) then
    begin
      MsgBox('El puerto debe ser un número entre 1 y 65535.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function JsonEscape(S: String): String;
begin
  StringChangeEx(S, '\', '\\', True);
  StringChangeEx(S, '"', '\"', True);
  Result := S;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Seed, Remote: String;
  ResultCode: Integer;
begin
  // Cerrar la instancia en ejecución antes de copiar: al actualizar, el .exe estaría en uso.
  if CurStep = ssInstall then
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/im {#MyAppExeName} /f', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('remotepanel') then Remote := 'true' else Remote := 'false';
    Seed := '{"port": ' + GetPort('') + ', "api_key": "' + JsonEscape(Trim(ConfigPage.Values[1])) +
            '", "remote_panel_enabled": ' + Remote + '}';
    SaveStringToFile(ExpandConstant('{commonappdata}\DimabePrintBridge\installer-seed.json'), Seed, False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if MsgBox('¿Borrar también la configuración y la bitácora de impresión?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{commonappdata}\DimabePrintBridge'), True, True, True);
end;
