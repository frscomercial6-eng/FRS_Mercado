#define MyAppName "FRS Mercado"
#define MyAppVersion "1.0.9"
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
OutputBaseFilename=FRS_Mercado_Setup
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
Name: "instalaracbr"; Description: "Instalar ACBrMonitor (motor fiscal)"; GroupDescription: "Componentes adicionais:"; Flags: checkedonce

[Files]
Source: "dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\*"; DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "version.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "EULA.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "_build_support\credentials.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\google-services.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\checklist_homologacao.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "_build_support\acbr\ACBrMonitor_Installer.exe"; DestDir: "{app}\instala"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\instala\ACBrMonitor_Installer.exe"; Parameters: "/VERYSILENT /NORESTART"; Description: "Instalar ACBrMonitor"; Flags: waituntilterminated runhidden skipifsilent skipifdoesntexist; Tasks: instalaracbr
Filename: "{app}\{#MyAppExeName}"; Description: "Executar {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function NextButtonClick(CurPageID: Integer): Boolean;
var
	Resp: Integer;
begin
	Result := True;
	if (CurPageID = wpSelectTasks) and (not WizardIsTaskSelected('instalaracbr')) then
	begin
		Resp := MsgBox(
			'ATENCAO: Voce esta desativando o componente de emissao fiscal. Caso esta opcao seja desmarcada, nao sera possivel emitir Nota Fiscal nem realizar a busca de XML no banco de dados. Deseja realmente prosseguir?',
			mbConfirmation,
			MB_YESNO
		);
		if Resp = IDNO then
			Result := False;
	end;
end;
