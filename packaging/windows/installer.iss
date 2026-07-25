#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef OutputDir
  #error OutputDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef ProjectRoot
  #error ProjectRoot muss beim Aufruf von ISCC gesetzt werden.
#endif

#define AppName "E-Rechnungs-Prüfer"
#define AppExeName "E-Rechnungs-Pruefer.exe"

[Setup]
AppId={{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=E-Rechnungs-Pruefer contributors
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName}
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\E-Rechnungs-Pruefer
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=E-Rechnungs-Pruefer-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile={#ProjectRoot}\LICENSE
InfoAfterFile={#ProjectRoot}\THIRD_PARTY.md

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"; Flags: unchecked
Name: "autostart"; Description: "Bei Windows-Anmeldung automatisch starten"; GroupDescription: "Automatisierung:"; Flags: unchecked

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#AppExeName}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\THIRD_PARTY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "E-Rechnungs-Pruefer"; Flags: deletevalue; Check: not WizardIsTaskSelected('autostart')
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "E-Rechnungs-Pruefer"; ValueData: """{app}\{#AppExeName}"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--background"; Flags: nowait; Check: ShouldRestartBackgroundAfterUpdate
Filename: "{app}\{#AppExeName}"; Description: "{#AppName} starten"; Flags: nowait postinstall skipifsilent; Check: not ShouldRestartBackgroundAfterUpdate

[UninstallDelete]
Type: files; Name: "{localappdata}\E-Rechnungs-Pruefer\runtime.json"
Type: files; Name: "{localappdata}\E-Rechnungs-Pruefer\api-token.txt"
Type: files; Name: "{localappdata}\E-Rechnungs-Pruefer\startup-error.log"
Type: dirifempty; Name: "{localappdata}\E-Rechnungs-Pruefer"

[Code]
const
  AppMutexName = 'Local\E-Rechnungs-Pruefer-Desktop';
  BackendMutexName = 'Global\E-Rechnungs-Pruefer-Backend';
  ShutdownEventName = 'Local\E-Rechnungs-Pruefer-Desktop-Shutdown';
  EventModifyState = $0002;
  ShutdownTimeoutMilliseconds = 30000;
  ShutdownPollMilliseconds = 250;

var
  ShutdownPrepared: Boolean;
  RestartBackgroundAfterUpdate: Boolean;

function OpenEvent(DesiredAccess: DWORD; InheritHandle: BOOL; Name: String): Cardinal;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(EventHandle: Cardinal): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(Handle: Cardinal): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function SignalApplicationShutdown: Boolean;
var
  ShutdownHandle: Cardinal;
begin
  ShutdownHandle := OpenEvent(EventModifyState, False, ShutdownEventName);
  if ShutdownHandle = 0 then
  begin
    Log('Das Shutdown-Ereignis der laufenden Anwendung konnte nicht geöffnet werden.');
    Result := False;
    Exit;
  end;

  try
    Result := SetEvent(ShutdownHandle);
    if not Result then
      Log('Das Shutdown-Ereignis der laufenden Anwendung konnte nicht signalisiert werden.');
  finally
    CloseHandle(ShutdownHandle);
  end;
end;

function WaitForApplicationExit: Boolean;
var
  WaitedMilliseconds: Cardinal;
begin
  WaitedMilliseconds := 0;
  while CheckForMutexes(AppMutexName) and
        (WaitedMilliseconds < ShutdownTimeoutMilliseconds) do
  begin
    Sleep(ShutdownPollMilliseconds);
    WaitedMilliseconds := WaitedMilliseconds + ShutdownPollMilliseconds;
  end;
  Result := not CheckForMutexes(AppMutexName);
end;

function StopRunningApplication(var WasRunning: Boolean): String;
begin
  WasRunning := CheckForMutexes(AppMutexName);
  if not WasRunning then
  begin
    Result := '';
    Exit;
  end;

  if not SignalApplicationShutdown then
  begin
    Result :=
      'Die laufende Anwendung unterstützt das kontrollierte Beenden noch nicht. ' +
      'Beenden Sie den E-Rechnungs-Prüfer einmalig über das Symbol im Infobereich ' +
      'und starten Sie den Vorgang anschließend erneut.';
    Exit;
  end;

  if not WaitForApplicationExit then
  begin
    Result :=
      'Der laufende E-Rechnungs-Prüfer konnte nicht innerhalb von 30 Sekunden ' +
      'kontrolliert beendet werden. Beenden Sie ihn über das Symbol im Infobereich ' +
      'und starten Sie den Vorgang anschließend erneut.';
    Exit;
  end;

  Result := '';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  WasRunning: Boolean;
  ExistingInstallation: Boolean;
begin
  if ShutdownPrepared then
  begin
    Result := '';
    Exit;
  end;

  if RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Services\ERechnungsPrueferService') then
  begin
    Result :=
      'Der systemweite E-Rechnungs-Prüfer-Dienst ist installiert. Desktop- und Dienstmodus sind alternative ' +
      'Betriebsarten. Deinstallieren Sie den Dienst oder verwenden Sie dessen Öffnen-Client.';
    Exit;
  end;

  if CheckForMutexes(BackendMutexName) and not CheckForMutexes(AppMutexName) then
  begin
    Result :=
      'Der E-Rechnungs-Prüfer-Dienst läuft bereits. Desktop- und Dienstmodus dürfen nicht parallel ' +
      'betrieben werden. Stoppen Sie den Dienst oder verwenden Sie den Dienst-Installer.';
    Exit;
  end;

  ExistingInstallation := FileExists(ExpandConstant('{app}\{#AppExeName}'));
  Result := StopRunningApplication(WasRunning);
  if Result = '' then
  begin
    ShutdownPrepared := True;
    RestartBackgroundAfterUpdate := WasRunning and ExistingInstallation;
  end;
end;

function ShouldRestartBackgroundAfterUpdate: Boolean;
begin
  Result := RestartBackgroundAfterUpdate and WizardIsTaskSelected('autostart');
end;

function InitializeUninstall: Boolean;
var
  WasRunning: Boolean;
  ErrorMessage: String;
begin
  ErrorMessage := StopRunningApplication(WasRunning);
  Result := ErrorMessage = '';
  if not Result then
  begin
    Log(ErrorMessage);
    if not UninstallSilent then
      MsgBox(ErrorMessage, mbError, MB_OK);
  end;
end;
