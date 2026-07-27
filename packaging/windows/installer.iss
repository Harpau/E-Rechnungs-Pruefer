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
  SetupUninstallMutexName = 'Global\E-Rechnungs-Pruefer-Setup-Uninstall';
  ShutdownEventName = 'Local\E-Rechnungs-Pruefer-Desktop-Shutdown';
  EventModifyState = $0002;
  WaitObject0 = $00000000;
  WaitAbandoned0 = $00000080;
  WaitTimeout = $00000102;
  WaitFailed = $FFFFFFFF;
  ShutdownTimeoutMilliseconds = 30000;
  ShutdownPollMilliseconds = 250;

var
  ShutdownPrepared: Boolean;
  RestartBackgroundAfterUpdate: Boolean;
  SetupUninstallMutexHandle: Cardinal;
  SetupUninstallMutexOwned: Boolean;

function CreateMutexW(
  SecurityAttributes: Integer; InitialOwner: BOOL; Name: String): Cardinal;
  external 'CreateMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: Cardinal; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: Cardinal): BOOL;
  external 'ReleaseMutex@kernel32.dll stdcall';
function OpenEvent(DesiredAccess: DWORD; InheritHandle: BOOL; Name: String): Cardinal;
  external 'OpenEventW@kernel32.dll stdcall';
function SetEvent(EventHandle: Cardinal): BOOL;
  external 'SetEvent@kernel32.dll stdcall';
function CloseHandle(Handle: Cardinal): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';

function AcquireSetupUninstallMutex: Boolean;
var
  ErrorCode: LongInt;
  WaitResult: Cardinal;
begin
  Result := False;
  if SetupUninstallMutexOwned then
  begin
    Result := True;
    Exit;
  end;
  if SetupUninstallMutexHandle <> 0 then
  begin
    Log('Die gemeinsame Installationssperre besitzt einen widersprüchlichen lokalen Zustand.');
    Exit;
  end;

  SetupUninstallMutexHandle :=
    CreateMutexW(0, False, SetupUninstallMutexName);
  ErrorCode := DLLGetLastError;
  if SetupUninstallMutexHandle = 0 then
  begin
    Log(
      'Die gemeinsame Installationssperre konnte nicht geöffnet werden ' +
      '(Windows-Fehler ' + IntToStr(ErrorCode) + ').');
    Exit;
  end;

  WaitResult := WaitForSingleObject(SetupUninstallMutexHandle, 0);
  ErrorCode := DLLGetLastError;
  if (WaitResult = WaitObject0) or (WaitResult = WaitAbandoned0) then
  begin
    SetupUninstallMutexOwned := True;
    if WaitResult = WaitAbandoned0 then
      Log('Eine abgebrochene Installationssperre wurde übernommen.');
    Result := True;
    Exit;
  end;

  if WaitResult = WaitFailed then
    Log(
      'Die gemeinsame Installationssperre konnte nicht geprüft werden ' +
      '(Windows-Fehler ' + IntToStr(ErrorCode) + ').')
  else if WaitResult = WaitTimeout then
    Log('Eine andere Installation oder Deinstallation ist bereits aktiv.')
  else
    Log(
      'Die gemeinsame Installationssperre lieferte einen unbekannten ' +
      'Wartezustand (' + IntToStr(WaitResult) + ').');
  CloseHandle(SetupUninstallMutexHandle);
  SetupUninstallMutexHandle := 0;
end;

procedure ReleaseSetupUninstallMutex;
begin
  if SetupUninstallMutexHandle = 0 then
    Exit;
  if SetupUninstallMutexOwned then
    ReleaseMutex(SetupUninstallMutexHandle);
  SetupUninstallMutexOwned := False;
  CloseHandle(SetupUninstallMutexHandle);
  SetupUninstallMutexHandle := 0;
end;

function ServiceFootprintExists: Boolean;
begin
  Result :=
    RegKeyExists(
      HKLM64,
      'SYSTEM\CurrentControlSet\Services\ERechnungsPrueferService') or
    RegKeyExists(
      HKLM64,
      'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8824D15C-7F4E-4CB2-B957-FBC26B923363}_is1') or
    DirExists(ExpandConstant('{autopf64}\E-Rechnungs-Pruefer-Dienst'));
end;

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
  if not AcquireSetupUninstallMutex then
  begin
    Result :=
      'Eine andere Installation oder Deinstallation ist aktiv oder die gemeinsame ' +
      'Vorgangssperre konnte nicht sicher erworben werden. Versuchen Sie es nach ' +
      'Abschluss des anderen Vorgangs erneut.';
    Exit;
  end;

  if ShutdownPrepared then
  begin
    Result := '';
    Exit;
  end;

  if ServiceFootprintExists then
  begin
    Result :=
      'Eine vorhandene oder unvollständig entfernte Dienst-Version wurde gefunden. ' +
      'Deinstallieren Sie den E-Rechnungs-Prüfer Dienst unter "Installierte Apps" ' +
      'vollständig und starten Sie dieses Desktop-Setup anschließend erneut.';
    Exit;
  end;

  if CheckForMutexes(BackendMutexName) and not CheckForMutexes(AppMutexName) then
  begin
    Result :=
      'Der systemweite E-Rechnungs-Prüfer-Dienst oder ein fremder Backendprozess läuft. ' +
      'Desktop- und Dienstmodus dürfen nicht parallel betrieben werden. Deinstallieren Sie ' +
      'zunächst die Dienst-Version und starten Sie dieses Desktop-Setup anschließend erneut.';
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

procedure DeinitializeSetup;
begin
  ReleaseSetupUninstallMutex;
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
  if not AcquireSetupUninstallMutex then
  begin
    Result := False;
    if not UninstallSilent then
      MsgBox(
        'Eine andere Installation oder Deinstallation ist aktiv. ' +
        'Versuchen Sie es nach deren Abschluss erneut.',
        mbError, MB_OK);
    Exit;
  end;
  ErrorMessage := StopRunningApplication(WasRunning);
  Result := ErrorMessage = '';
  if not Result then
  begin
    Log(ErrorMessage);
    if not UninstallSilent then
      MsgBox(ErrorMessage, mbError, MB_OK);
    ReleaseSetupUninstallMutex;
  end;
end;

procedure DeinitializeUninstall;
begin
  ReleaseSetupUninstallMutex;
end;
