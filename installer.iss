; Instalador do Cockpit Monitor (Inno Setup 6 — https://jrsoftware.org/isinfo.php)
; Gerado automaticamente pelo build_exe.bat se o Inno Setup estiver instalado.

#define AppName "Cockpit Monitor"
#define AppVersion "1.0"
#define AppExe "CockpitMonitor.exe"

[Setup]
AppId={{6E0B7D52-4C1F-4C7A-9C2B-C0C4B1A7E1F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Cockpit Monitor
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=dist
OutputBaseFilename=CockpitMonitor_Setup
SetupIconFile=cockpit.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "ptbr"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na Área de Trabalho"; GroupDescription: "Atalhos:"
Name: "autostart"; Description: "Iniciar junto com o Windows (só os mini-relógios na barra)"; GroupDescription: "Inicialização:"

[Files]
Source: "dist\CockpitMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Desinstalar {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "--enable-autostart"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "Abrir o {#AppName} agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN CockpitMonitor /F"; Flags: runhidden; RunOnceId: "DelTask"
