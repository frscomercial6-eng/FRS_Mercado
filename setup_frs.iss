#define MyAppName "FRS Mercado"
#define MyAppVersion "1.0.4"
#define MyAppPublisher "FRS Solutions"
#define MyAppExeName "FRS_Mercado.exe"
#define PaymentURL "https://invoice.infinitepay.io/plans/frsoficinadepesca/avka57U38g"

[Setup]
AppId={{B4A3A6E8-7D9A-4D48-B9B1-88233F1EF8CE}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=EULA.txt
OutputDir=installer
OutputBaseFilename=FRS_Mercado_Setup_1.0.4
SetupIconFile=assets\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "portuguesebrazilian"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalhos na area de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "version.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "EULA.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "_build_support\credentials.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\google-services.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\checklist_homologacao.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Executar {#MyAppName}"; Flags: nowait postinstall skipifsilent
