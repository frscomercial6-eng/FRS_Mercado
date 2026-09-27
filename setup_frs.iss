#define MyAppName "FRS Mercado"
#define MyAppVersion "1.0.20"
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
Name: "instalaracbr"; Description: "Instalar e configurar ACBrMonitor (motor fiscal)"; GroupDescription: "Componentes adicionais:"; Flags: checkedonce

[Files]
Source: "dist\FRS_Mercado\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\logo.ico"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "assets\frsMercado.ico"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "assets\frsMercado.jpeg"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist
Source: "version.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "EULA.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "_build_support\checklist_homologacao.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "_build_support\data\*"; DestDir: "{app}\data"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "_build_support\acbr\ACBrMonitor_Installer.exe"; DestDir: "{app}\instala"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
; Permissoes de escrita para usuarios padrao: o ACBrMonitor (motor fiscal de terceiros)
; pode gravar seu proprio log/ini junto do executavel mesmo quando instalado em Program Files.
Name: "{app}\instala"; Permissions: users-modify
Name: "{app}\data"; Permissions: users-modify

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\logo.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\logo.ico"; Tasks: desktopicon

[Run]
; O instalador do ACBr roda TAMBEM quando o FRS e instalado em /VERYSILENT ou
; /SILENT. A flag skipifsilent era omitida de proposito aqui porque impedia a
; instalacao silenciosa do motor fiscal (o task instalaracbr nunca executava em
; modo silencioso, deixando o cliente sem emissao de NF-e).
; runhidden mantem a execucao sem janela; waituntilterminated garante que o
; Inno so avancs apos o instalador do ACBr terminar, permitindo ao
; RegistrarResultadoAcbr verificar o resultado real.
Filename: "{app}\instala\ACBrMonitor_Installer.exe"; Parameters: "/VERYSILENT /NORESTART /LOG={app}\instala\acbr_install.log"; Description: "Instalar ACBrMonitor"; Flags: runhidden waituntilterminated skipifdoesntexist; Tasks: instalaracbr; Check: ShouldRunAcbrInstaller
Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Description: "Executar {#MyAppName}"; Flags: nowait postinstall skipifsilent skipifdoesntexist

[Code]
procedure CriarConfiguracaoInicial;
var
	DataDir, FiscalIn, FiscalOut, ExportDir, ConfigFile, Json: string;
begin
	DataDir := ExpandConstant('{userappdata}\FRS_Mercado\data');
	FiscalIn := DataDir + '\fiscal_in';
	FiscalOut := DataDir + '\fiscal_out';
	ExportDir := DataDir + '\exportacao_fiscal';
	ConfigFile := DataDir + '\config.json';

	ForceDirectories(FiscalIn);
	ForceDirectories(FiscalOut);
	ForceDirectories(ExportDir);

	if not FileExists(ConfigFile) then
	begin
		Json :=
			'{' + #13#10 +
			'  "razao_social": "",' + #13#10 +
			'  "nome_estabelecimento": "",' + #13#10 +
			'  "fiscal_ativo": false,' + #13#10 +
			'  "auto_update_enabled": true,' + #13#10 +
			'  "pasta_entrada_fiscal": "' + FiscalIn + '",' + #13#10 +
			'  "pasta_retorno_fiscal": "' + FiscalOut + '",' + #13#10 +
			'  "pasta_exportacao_fiscal": "' + ExportDir + '"' + #13#10 +
			'}';
		StringChangeEx(Json, '\', '\\', True);
		SaveStringToFile(ConfigFile, Json, False);
	end;
end;

// Declaracao antecipada: o Pascal Script do Inno Setup exige que a procedure
// esteja DEFINIDA antes de ser chamada em outra procedure.
procedure RegistrarResultadoAcbr; forward;

// Localiza o MOTOR REAL do ACBrMonitor (nao e o instalador, nao e .exe
// renomeado). O instalador oficial 1.4.0.467 grava em C:\ACBrMonitorPLUS.
function LocalizarMotorAcbr: string;
var
Nome: string;
begin
Result := '';
Nome := 'ACBrMonitor.exe';
if FileExists('C:\ACBrMonitorPLUS\' + Nome) then
begin
Result := 'C:\ACBrMonitorPLUS\' + Nome;
Exit;
end;
if FileExists(ExpandConstant('{app}\instala\') + Nome) then
begin
Result := ExpandConstant('{app}\instala\') + Nome;
Exit;
end;
end;

// Decide se o instalador do ACBr deve rodar. O instalador oficial 1.4.0.467
// devolve EXIT CODE 2 quando o ACBr ja esta instalado, e o Inno Setup trata
// qualquer codigo diferente de zero como falha fatal, abortando a instalacao
// INTEIRA do FRS. O ACBr e um componente OPCIONAL: o FRS nunca pode ser
// impedido de instalar por causa dele. Aqui retornamos False apenas quando o
// motor ja existe (nao ha o que instalar); caso contrario o instalador roda e,
// mesmo se falhar, o resultado e apenas registrado - nunca aborta o FRS.
function ShouldRunAcbrInstaller: Boolean;
begin
Result := not WizardIsTaskSelected('instalaracbr');
if Result then
Exit;
Result := LocalizarMotorAcbr = '';
end;

// Verifica o resultado da instalacao do ACBr e grava um status legivel.
// O task 'instalaracbr' roda silenciosamente; sem esta verificacao uma falha
// so apareceria quando o usuario abrisse o aplicativo pela primeira vez.
procedure RegistrarResultadoAcbr;
var
	Instalador, Motor, LogArq, StatusArq: string;
	Linha: string;
begin
	StatusArq := ExpandConstant('{app}\instala\acbr_status.txt');
	Instalador := ExpandConstant('{app}\instala\ACBrMonitor_Installer.exe');
	LogArq := ExpandConstant('{app}\instala\acbr_install.log');

	if not WizardIsTaskSelected('instalaracbr') then
	begin
		Linha := 'ACBR=DESSELECTADO; usuario optou por nao instalar o motor fiscal.';
		SaveStringToFile(StatusArq, Linha + #13#10, False);
		Exit;
	end;

	if not FileExists(Instalador) then
	begin
		Linha := 'AVISO: instalador do ACBrMonitor nao encontrado em {app}\instala.'
			+ ' O componente fiscal nao sera instalado; o aplicativo continuara'
			+ ' funcionando, porem sem emissao de documentos fiscais.';
		SaveStringToFile(StatusArq, Linha + #13#10, False);
		Exit;
	end;

Motor := LocalizarMotorAcbr;

if Motor = '' then
begin
Linha := 'AVISO: o instalador do ACBrMonitor foi executado, mas o MOTOR REAL'
+ ' nao foi localizado (esperado C:\ACBrMonitorPLUS\ACBrMonitor.exe).'
+ ' Consulte acbr_install.log. O aplicativo tentara configurar/instalar'
+ ' o motor no primeiro uso.';
if FileExists(LogArq) then
Linha := Linha + ' Log gerado em: ' + LogArq;
SaveStringToFile(StatusArq, Linha + #13#10, False);
Exit;
end;

	Linha := 'OK: motor fiscal ACBrMonitor disponivel (' + Motor + ').';
	if FileExists(LogArq) then
		Linha := Linha + #13#10 + 'Log do instalador: ' + LogArq;
	SaveStringToFile(StatusArq, Linha + #13#10, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
	if CurStep = ssPostInstall then
	begin
		CriarConfiguracaoInicial;
		RegistrarResultadoAcbr;
	end;
end;

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
