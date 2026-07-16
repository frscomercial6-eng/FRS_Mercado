#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#ifndef AppSourceDir
  #define AppSourceDir "_flet_windows_stage\\app"
#endif

#ifndef ACBrInstallerName
  #define ACBrInstallerName "ACBrMonitor.exe"
#endif

#define MyAppName "FRS Mercado"
#define MyAppPublisher "FRS Solutions"
#define MyAppExeName "mercado.exe"

[Setup]
AppId={{E5C04110-4F28-44C3-986C-C2B3E8D8CF4A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=FRS_Mercado_FletBundle_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "portuguesebrazilian"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalhos na area de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked
Name: "instalaracbr"; Description: "Instalar ACBrMonitor junto com o sistema"; GroupDescription: "Componentes adicionais:"; Flags: checkedonce

[Files]
Source: "{#AppSourceDir}\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "_flet_windows_stage\\acbr\\{#ACBrInstallerName}"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\{#ACBrInstallerName}"; Parameters: "/VERYSILENT /NORESTART"; Description: "Instalar ACBrMonitor"; Flags: waituntilterminated runhidden; Tasks: instalaracbr
Filename: "{app}\{#MyAppExeName}"; Description: "Executar {#MyAppName}"; Flags: nowait postinstall skipifsilent
