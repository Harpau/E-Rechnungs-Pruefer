#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef ServiceSourceDir
  #error ServiceSourceDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef OpenClientFile
  #error OpenClientFile muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef OutputDir
  #error OutputDir muss beim Aufruf von ISCC gesetzt werden.
#endif
#ifndef ProjectRoot
  #error ProjectRoot muss beim Aufruf von ISCC gesetzt werden.
#endif

#define AppName "E-Rechnungs-Prüfer Dienst"
#define ServiceName "ERechnungsPrueferService"
#define ServiceExeName "E-Rechnungs-Pruefer-Dienst.exe"
#define OpenClientExeName "E-Rechnungs-Pruefer-Oeffnen.exe"

[Setup]
AppId={{8824D15C-7F4E-4CB2-B957-FBC26B923363}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=E-Rechnungs-Pruefer contributors
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName}
VersionInfoProductName={#AppName}
DefaultDirName={autopf64}\E-Rechnungs-Pruefer-Dienst
DefaultGroupName=E-Rechnungs-Prüfer
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=E-Rechnungs-Pruefer-{#AppVersion}-Windows-x64-Dienst-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\service\{#OpenClientExeName}
LicenseFile={#ProjectRoot}\LICENSE
InfoAfterFile={#ProjectRoot}\THIRD_PARTY.md

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "systemstart"; Description: "Beim Systemstart starten (verzögert)"; GroupDescription: "Dienststart:"; Flags: checkedonce

[Files]
Source: "{#OpenClientFile}"; Flags: dontcopy noencryption
Source: "{#ServiceSourceDir}\*"; DestDir: "{app}\service.new"; Flags: ignoreversion recursesubdirs createallsubdirs uninsneveruninstall
Source: "{#OpenClientFile}"; DestDir: "{app}\service.new"; Flags: ignoreversion uninsneveruninstall
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{app}\service.new"; Flags: ignoreversion uninsneveruninstall
Source: "{#ProjectRoot}\THIRD_PARTY.md"; DestDir: "{app}\service.new"; Flags: ignoreversion uninsneveruninstall; AfterInstall: ConfigureInstalledService
Source: "{#ProjectRoot}\LICENSE"; DestDir: "{code:PropagateServiceConfigurationFailure}"; Flags: ignoreversion; Check: ServiceConfigurationHasFailed

[Icons]
Name: "{group}\E-Rechnungs-Prüfer öffnen"; Filename: "{app}\service\{#OpenClientExeName}"; WorkingDir: "{app}\service"

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
const
  ServiceName = '{#ServiceName}';
  BackendMutexName = 'Global\E-Rechnungs-Pruefer-Backend';
  SetupUninstallMutexName = 'Global\E-Rechnungs-Pruefer-Setup-Uninstall';
  WaitObject0 = $00000000;
  WaitAbandoned0 = $00000080;
  WaitTimeout = $00000102;
  WaitFailed = $FFFFFFFF;
  ServiceWaitMilliseconds = 330000;
  ServicePollMilliseconds = 250;
  ServiceQueryError = -1;
  ServiceAbsent = 0;
  ServicePresent = 1;
  ReconcileNone = 0;
  ReconcileRollback = 10;
  ReconcileCommit = 11;
  ReconcileCleanup = 12;
  SetupHwndTopMost = -1;
  SetupHwndNotTopMost = -2;
  SetupSwRestore = 9;
  SetupSwpNoSize = $0001;
  SetupSwpNoMove = $0002;
  SetupSwpNoActivate = $0010;
  SetupSwpShowWindow = $0040;
  InitialWizardFallbackCleanupMilliseconds = 10000;

var
  ServiceExistedBefore: Boolean;
  ServiceWasRunning: Boolean;
  ServicePrepared: Boolean;
  ServiceTemporarilyDisabled: Boolean;
  ServiceCreatedBySetup: Boolean;
  ServiceBundleBackupCreated: Boolean;
  InstallSucceeded: Boolean;
  ServiceTransactionPrepared: Boolean;
  TransactionCommitStarted: Boolean;
  OriginalStartMode: String;
  ServiceMetadataCaptured: Boolean;
  PurgeMachineData: Boolean;
  UninstallStateValidated: Boolean;
  SetupUninstallMutexHandle: Cardinal;
  SetupUninstallMutexOwned: Boolean;
  InitialWizardActivationScheduled: Boolean;
  InitialWizardActivationCompleted: Boolean;
  InitialWizardActivationShuttingDown: Boolean;
  InitialWizardActivationTimer: UINT_PTR;
  InitialWizardFallbackTopMost: Boolean;
  InitialWizardFallbackCleanupTimer: UINT_PTR;
  ServiceConfigurationFailed: Boolean;
  ServiceConfigurationFailureReason: String;

function CreateMutexW(
  SecurityAttributes: Integer; InitialOwner: BOOL; Name: String): Cardinal;
  external 'CreateMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle: Cardinal; Milliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: Cardinal): BOOL;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: Cardinal): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
function SetTimer(
  Window: HWND; TimerID: UINT_PTR; Elapse: UINT; TimerProcedure: LongWord): UINT_PTR;
  external 'SetTimer@user32.dll stdcall';
function KillTimer(Window: HWND; TimerID: UINT_PTR): BOOL;
  external 'KillTimer@user32.dll stdcall';
function ShowWindow(Window: HWND; ShowCommand: Integer): BOOL;
  external 'ShowWindow@user32.dll stdcall';
function SetForegroundWindow(Window: HWND): BOOL;
  external 'SetForegroundWindow@user32.dll stdcall';
function GetForegroundWindow: HWND;
  external 'GetForegroundWindow@user32.dll stdcall';
function SetWindowPos(
  Window: HWND; InsertAfter: HWND; X, Y, Width, Height: Integer;
  Flags: UINT): BOOL;
  external 'SetWindowPos@user32.dll stdcall';

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
    Log(
      'Die Installations-/Deinstallationssperre besitzt einen ' +
      'widersprüchlichen lokalen Zustand.');
    Exit;
  end;

  { Global is intentional: elevated setup and uninstall must serialize even
    when launched from different interactive Windows sessions. }
  SetupUninstallMutexHandle :=
    CreateMutexW(0, False, SetupUninstallMutexName);
  ErrorCode := DLLGetLastError;
  if SetupUninstallMutexHandle = 0 then
  begin
    Log(
      'Die systemweite Installations-/Deinstallationssperre konnte nicht ' +
      'geöffnet werden (Windows-Fehler ' + IntToStr(ErrorCode) + ').');
    Exit;
  end;

  WaitResult := WaitForSingleObject(SetupUninstallMutexHandle, 0);
  ErrorCode := DLLGetLastError;
  if (WaitResult = WaitObject0) or (WaitResult = WaitAbandoned0) then
  begin
    SetupUninstallMutexOwned := True;
    if WaitResult = WaitAbandoned0 then
      Log(
        'Eine abgebrochene Installations-/Deinstallationssperre wurde ' +
        'übernommen; die persistente Recovery wird vor Änderungen geprüft.');
    Result := True;
    Exit;
  end;

  if WaitResult = WaitFailed then
    Log(
      'Die systemweite Installations-/Deinstallationssperre konnte nicht ' +
      'geprüft werden (Windows-Fehler ' + IntToStr(ErrorCode) + ').')
  else if WaitResult = WaitTimeout then
    Log(
      'Eine andere Installation oder Deinstallation ist ' +
      'bereits aktiv.')
  else
    Log(
      'Die systemweite Installations-/Deinstallationssperre lieferte einen ' +
      'unbekannten Wartezustand (' + IntToStr(WaitResult) + ').');
  if not CloseHandle(SetupUninstallMutexHandle) then
  begin
    ErrorCode := DLLGetLastError;
    Log(
      'Das nicht erworbene Mutex-Handle konnte nicht geschlossen werden ' +
      '(Windows-Fehler ' + IntToStr(ErrorCode) + ').');
  end;
  SetupUninstallMutexHandle := 0;
end;

procedure ReleaseSetupUninstallMutex;
var
  ErrorCode: LongInt;
begin
  if SetupUninstallMutexHandle = 0 then
    Exit;
  if SetupUninstallMutexOwned then
  begin
    if not ReleaseMutex(SetupUninstallMutexHandle) then
    begin
      ErrorCode := DLLGetLastError;
      Log(
        'Die Installations-/Deinstallationssperre konnte am Prozessende nicht ' +
        'freigegeben werden (Windows-Fehler ' + IntToStr(ErrorCode) + ').');
    end;
  end;
  SetupUninstallMutexOwned := False;
  if not CloseHandle(SetupUninstallMutexHandle) then
  begin
    ErrorCode := DLLGetLastError;
    Log(
      'Das Installations-/Deinstallations-Mutex-Handle konnte am Prozessende ' +
      'nicht geschlossen werden (Windows-Fehler ' + IntToStr(ErrorCode) + ').');
  end;
  SetupUninstallMutexHandle := 0;
end;

function ServiceLiveDir: String;
begin
  Result := ExpandConstant('{app}\service');
end;

function ServiceNewDir: String;
begin
  Result := ExpandConstant('{app}\service.new');
end;

function ServiceRollbackDir: String;
begin
  Result := ExpandConstant('{app}\service.rollback');
end;

function ServiceObsoleteDir: String;
begin
  Result := ExpandConstant('{app}\service.obsolete');
end;

function ExpectedServiceExe: String;
begin
  Result := ServiceLiveDir + '\{#ServiceExeName}';
end;

procedure RemoveEmptyInstallRootAfterRollback;
begin
  { Non-recursive by design: an update or any unknown content stays untouched. }
  RemoveDir(ExpandConstant('{app}'));
end;

function InternalOpenClient: String;
begin
  Result := ExpandConstant('{tmp}\{#OpenClientExeName}');
  if not FileExists(Result) then
    Result := ExpandConstant('{app}\service\{#OpenClientExeName}');
end;

function DeleteTreeIfPresent(Path: String; Description: String): Boolean;
begin
  Result := True;
  if DirExists(Path) then
  begin
    Result := DelTree(Path, True, True, True);
    if not Result then
      Log(Description + ' konnte nicht vollständig gelöscht werden: ' + Path);
  end;
end;

function QueryService(var ServiceObject: Variant): Integer;
var
  Locator: Variant;
  WmiService: Variant;
  Services: Variant;
begin
  Result := ServiceQueryError;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    WmiService := Locator.ConnectServer('.', 'root\cimv2');
    Services := WmiService.ExecQuery(
      'SELECT Name, State, StartMode, PathName, StartName FROM Win32_Service WHERE Name=''' + ServiceName + '''');
    Result := ServiceAbsent;
    if Integer(Services.Count) > 0 then
    begin
      ServiceObject := Services.ItemIndex(0);
      Result := ServicePresent;
    end;
  except
    Log('Der SCM-Zustand konnte nicht über WMI gelesen werden: ' + GetExceptionMessage);
  end;
end;

function ServiceHasState(ExpectedState: String): Boolean;
var
  ServiceObject: Variant;
begin
  Result := (QueryService(ServiceObject) = ServicePresent) and
    (CompareText(String(ServiceObject.State), ExpectedState) = 0);
end;

function ServiceBelongsToThisInstallation(ServiceObject: Variant): Boolean;
begin
  Result :=
    (CompareText(
      RemoveQuotes(Trim(String(ServiceObject.PathName))),
      ExpandConstant('{app}\service\{#ServiceExeName}')) = 0) and
    (CompareText(String(ServiceObject.StartName), 'NT AUTHORITY\LocalService') = 0);
end;

function ServiceStartModeIsSupported(StartMode: String): Boolean;
begin
  Result := CompareText(StartMode, 'Auto') = 0;
  if not Result then
    Result := CompareText(StartMode, 'Manual') = 0;
  if not Result then
    Result := CompareText(StartMode, 'Disabled') = 0;
end;

function ServiceBaselineIsRollbackable(ServiceState, StartMode: String): Boolean;
begin
  Result :=
    not ((CompareText(ServiceState, 'Running') = 0) and
      (CompareText(StartMode, 'Disabled') = 0));
end;

function ServiceStateIsSupported(ServiceState: String): Boolean;
begin
  Result := CompareText(ServiceState, 'Running') = 0;
  if not Result then
    Result := CompareText(ServiceState, 'Stopped') = 0;
end;

function WaitForServiceState(ExpectedState: String; TimeoutMilliseconds: Cardinal): Boolean;
var
  Waited: Cardinal;
begin
  Waited := 0;
  while Waited < TimeoutMilliseconds do
  begin
    if ServiceHasState(ExpectedState) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(ServicePollMilliseconds);
    Waited := Waited + ServicePollMilliseconds;
  end;
  Result := ServiceHasState(ExpectedState);
end;

function WaitForServiceRemoval(TimeoutMilliseconds: Cardinal): Boolean;
var
  Waited: Cardinal;
  ServiceObject: Variant;
  QueryResult: Integer;
begin
  Waited := 0;
  while Waited < TimeoutMilliseconds do
  begin
    QueryResult := QueryService(ServiceObject);
    if QueryResult = ServiceAbsent then
    begin
      Result := True;
      Exit;
    end;
    Sleep(ServicePollMilliseconds);
    Waited := Waited + ServicePollMilliseconds;
  end;
  Result := QueryService(ServiceObject) = ServiceAbsent;
end;

function ExecChecked(FileName: String; Parameters: String; Description: String): Boolean;
var
  ExitCode: Integer;
begin
  ExitCode := -1;
  Log(Description + ': ' + FileName);
  Result := Exec(
    FileName, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if Result then
    Result := ExitCode = 0;
  if not Result then
    Log(Description + ' ist fehlgeschlagen (Exitcode ' + IntToStr(ExitCode) + ').');
end;

function ExecWithExitCode(
  FileName: String; Parameters: String; Description: String; var ExitCode: Integer): Boolean;
begin
  ExitCode := -1;
  Log(Description + ': ' + FileName);
  Result := Exec(
    FileName, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if not Result then
    Log(Description + ' konnte nicht ausgeführt werden.');
end;

function CaptureOriginalServiceMetadata: Boolean;
begin
  Result := ExecChecked(
    InternalOpenClient,
    '--snapshot-service-metadata --expected-service-exe "' +
    ServiceLiveDir + '\{#ServiceExeName}"',
    'Ursprüngliche SCM-Metadaten sichern');
  if Result then
    ServiceMetadataCaptured := True;
end;

function ReconcileInterruptedServiceUninstall: Boolean;
begin
  Result := ExecChecked(
    InternalOpenClient,
    '--reconcile-service-uninstall --expected-service-exe "' +
    ServiceLiveDir + '\{#ServiceExeName}"',
    'Unterbrochene Dienst-Deinstallation zurücksetzen oder abschließen');
end;

function ClearOriginalServiceMetadata: Boolean;
begin
  Result := ExecChecked(
    InternalOpenClient,
    '--clear-service-metadata --expected-service-exe "' +
    ServiceLiveDir + '\{#ServiceExeName}"',
    'Geschützte SCM-Sicherung entfernen');
  if Result then
    ServiceMetadataCaptured := False;
end;

function Sc(Parameters: String; Description: String): Boolean;
begin
  Result := ExecChecked(ExpandConstant('{sys}\sc.exe'), Parameters, Description);
end;

function PrepareServiceBundleTransaction: String;
begin
  Result := '';
  if DirExists(ServiceNewDir) then
  begin
    Result :=
      'Ein Dienst-Staging einer anderen oder nicht vollständig reconcilierten ' +
      'Transaktion ist vorhanden.';
    Exit;
  end;
  if DirExists(ServiceRollbackDir) then
  begin
    Result :=
      'Ein Dienst-Rollback einer anderen oder nicht vollständig reconcilierten ' +
      'Transaktion ist vorhanden.';
    Exit;
  end;
  if DirExists(ServiceObsoleteDir) then
  begin
    Result :=
      'Ein abgelöster Dienststand einer nicht vollständig finalisierten ' +
      'Transaktion ist vorhanden.';
    Exit;
  end;
  if (not ServiceExistedBefore) and DirExists(ServiceLiveDir) then
  begin
    Result :=
      'Ein Dienst-Binärbaum ohne eindeutig zugehörigen SCM-Dienst wurde gefunden; ' +
      'die Installation bricht sicher ab.';
    Exit;
  end;
  if ServiceExistedBefore and
     not FileExists(ServiceLiveDir + '\{#ServiceExeName}') then
    Result := 'Der vorhandene Dienst besitzt keinen vollständigen Binärbaum.';
end;

procedure ActivateStagedServiceBundle;
begin
  if not DirExists(ServiceNewDir) or
     not FileExists(ServiceNewDir + '\{#ServiceExeName}') then
    RaiseException('Das bereitgestellte Dienstbundle ist unvollständig.');
  if DirExists(ServiceRollbackDir) then
    RaiseException('Ein nicht abgeschlossener Dienst-Rollback wurde gefunden.');
  if DirExists(ServiceLiveDir) then
  begin
    if not RenameFile(ServiceLiveDir, ServiceRollbackDir) then
      RaiseException('Der vorhandene Dienststand konnte nicht gesichert werden.');
    ServiceBundleBackupCreated := True;
  end
  else if ServiceExistedBefore then
    RaiseException('Der vorhandene Dienstbaum fehlt.');
  if not RenameFile(ServiceNewDir, ServiceLiveDir) then
  begin
    if ServiceBundleBackupCreated and
       not DirExists(ServiceLiveDir) and
       RenameFile(ServiceRollbackDir, ServiceLiveDir) then
      ServiceBundleBackupCreated := False;
    RaiseException('Das neue Dienstbundle konnte nicht aktiviert werden.');
  end;
end;

function CommitServiceBundle: Boolean;
begin
  Result := True;
  if not DeleteTreeIfPresent(ServiceNewDir, 'Das Dienst-Staging') then
    Result := False;
  if Result and DirExists(ServiceRollbackDir) then
  begin
    if DirExists(ServiceObsoleteDir) or
       not RenameFile(ServiceRollbackDir, ServiceObsoleteDir) then
      Result := False
  end;
end;

procedure FinalizeServiceBundle;
begin
  ServiceBundleBackupCreated := False;
  if not DeleteTreeIfPresent(
    ServiceObsoleteDir, 'Der alte Dienststand nach erfolgreichem Update') then
    Log('Der atomar abgelöste alte Dienststand wird beim nächsten Setup erneut bereinigt.');
end;

function RollbackServiceConfiguration: Boolean;
var
  ExitCode: Integer;
begin
  Result := True;
  if not ServiceTransactionPrepared then
    Exit;
  Result :=
    ExecWithExitCode(
      InternalOpenClient,
      '--mark-service-rollback-complete --expected-service-exe "' +
      ExpectedServiceExe + '"',
      'Dienstzustand transaktional zurücknehmen und beweisen',
      ExitCode) and
    (ExitCode = ReconcileRollback);
  if not Result then
  begin
    Log(
      'Der ursprüngliche Dienst-, Bundle- und Maschinenzustand konnte nicht ' +
      'vollständig bewiesen werden; der geschützte Dienstzustand bleibt pending.');
    Exit;
  end;
  ServiceTemporarilyDisabled := False;
  ServiceCreatedBySetup := False;
  ServiceBundleBackupCreated := False;
end;

function ClassifyInstallReconcile(var Direction: Integer): Boolean;
begin
  Result :=
    ExecWithExitCode(
      InternalOpenClient,
      '--prepare-install-reconcile --expected-service-exe "' +
      ExpectedServiceExe + '"',
      'Persistenten Installationszustand read-only klassifizieren',
      Direction) and
    ((Direction = ReconcileNone) or
     (Direction = ReconcileRollback) or
     (Direction = ReconcileCommit) or
     (Direction = ReconcileCleanup));
  if not Result then
    Log('Der persistente Installationszustand ist unbekannt oder widersprüchlich.');
end;

function FinishInstallReconcile(ExpectedDirection: Integer): Boolean;
var
  ExitCode: Integer;
begin
  Result :=
    ExecWithExitCode(
      InternalOpenClient,
      '--finish-install-reconcile --expected-service-exe "' +
      ExpectedServiceExe + '"',
      'Persistente Installations-Recovery beweisen oder finalisieren',
      ExitCode) and
    (ExitCode = ExpectedDirection);
  if not Result then
    Log(
      'Die persistente Installations-Recovery lieferte nicht den erwarteten ' +
      'Richtungsnachweis.');
end;

function FinishTerminalInstallTransaction: Boolean;
var
  Direction: Integer;
begin
  Result :=
    ExecWithExitCode(
      InternalOpenClient,
      '--finish-install-reconcile --expected-service-exe "' +
      ExpectedServiceExe + '"',
      'Terminalen Installationsbeleg finalisieren',
      Direction) and
    ((Direction = ReconcileCleanup) or (Direction = ReconcileNone));
  if not Result then
    Exit;
  Result := ClassifyInstallReconcile(Direction) and (Direction = ReconcileNone);
  if not Result then
    Log('Die abgeschlossene Installations-Transaktion blieb nach der Finalisierung sichtbar.');
  if Result then
    ServiceTransactionPrepared := False;
end;

function ReconcilePendingInstall: String;
var
  Direction: Integer;
begin
  Result := '';
  if not ClassifyInstallReconcile(Direction) then
  begin
    Result :=
      'Ein geschützter Installationszustand konnte nicht sicher klassifiziert werden. ' +
      'Die Installation verändert den Dienst nicht.';
    Exit;
  end;
  if Direction = ReconcileNone then
    Exit;
  if Direction = ReconcileCleanup then
  begin
    if not FinishInstallReconcile(ReconcileCleanup) or
       not ClassifyInstallReconcile(Direction) or
       (Direction <> ReconcileNone) then
      Result :=
        'Der Abschlussmarker einer früheren Installation konnte nicht sicher ' +
        'finalisiert werden.';
    Exit;
  end;

  ServiceTransactionPrepared := True;
  if not FinishInstallReconcile(Direction) then
  begin
    Result :=
      'Der Dienstzustand einer früheren Installation konnte nicht in seine ' +
      'persistente Zielrichtung überführt werden.';
    Exit;
  end;

  if Direction = ReconcileCommit then
    TransactionCommitStarted := True;

  FinalizeServiceBundle;
  if not FinishTerminalInstallTransaction then
  begin
    Result :=
      'Die fachliche Recovery ist abgeschlossen; ihr geschützter ' +
      'Transaktionsbeleg konnte noch nicht finalisiert werden.';
    Exit;
  end;
  if Direction = ReconcileRollback then
    RemoveEmptyInstallRootAfterRollback;
  TransactionCommitStarted := False;
end;

function BeginServiceTransition: Boolean;
var
  Parameters: String;
begin
  Parameters :=
    '--begin-service-transition --expected-service-exe "' +
    ExpectedServiceExe + '" --target-service-running ';
  if (not ServiceExistedBefore) or ServiceWasRunning then
    Parameters := Parameters + '1'
  else
    Parameters := Parameters + '0';
  Result := ExecChecked(
    InternalOpenClient,
    Parameters,
    'Diensttransition mit frischer SCM-, Bundle- und Maschinen-Baseline beginnen');
  if Result then
    ServiceTransactionPrepared := True;
end;

function MarkServiceCommitted: Boolean;
var
  ExitCode: Integer;
begin
  Result :=
    ExecWithExitCode(
      InternalOpenClient,
      '--mark-service-committed --expected-service-exe "' +
      ExpectedServiceExe + '"',
      'Neuen Dienstzustand prüfen und Commit-Grenze persistent setzen',
      ExitCode) and
    (ExitCode = ReconcileCommit);
end;

function RollbackPreparedInstall: Boolean;
begin
  Result := False;
  if ServiceTransactionPrepared and not RollbackServiceConfiguration then
    Exit;
  if ServiceTransactionPrepared and not FinishTerminalInstallTransaction then
    Exit;
  RemoveEmptyInstallRootAfterRollback;
  Result := True;
end;

function InspectExistingService: String;
var
  ServiceObject: Variant;
  QueryResult: Integer;
  ServiceState: String;
begin
  Result := '';
  QueryResult := QueryService(ServiceObject);
  if QueryResult = ServiceQueryError then
  begin
    Result := 'Der Windows-Dienststatus konnte nicht sicher ermittelt werden. Die Installation wird abgebrochen.';
    Exit;
  end;
  ServiceExistedBefore := QueryResult = ServicePresent;
  if not ServiceExistedBefore then
    Exit;

  if not ServiceBelongsToThisInstallation(ServiceObject) then
  begin
    Result :=
      'Ein gleichnamiger, aber nicht eindeutig zu dieser Installation gehörender Dienst wurde gefunden. ' +
      'Die Installation wird ohne Übernahme dieses Dienstes abgebrochen.';
    Exit;
  end;

  ServiceState := String(ServiceObject.State);
  if not ServiceStateIsSupported(ServiceState) then
  begin
    Result :=
      'Der vorhandene Dienst meldet keinen stabilen Zustand RUNNING oder STOPPED. ' +
      'Die Installation wird vor jeder Dienständerung abgebrochen.';
    Exit;
  end;
  OriginalStartMode := String(ServiceObject.StartMode);
  ServiceWasRunning := CompareText(ServiceState, 'Running') = 0;
  if not ServiceStartModeIsSupported(OriginalStartMode) then
  begin
    Result :=
      'Der vorhandene Dienst meldet einen unbekannten Starttyp. ' +
      'Die Installation wird vor jeder Dienständerung abgebrochen.';
    Exit;
  end;
  if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode) then
  begin
    Result :=
      'Der vorhandene Dienst läuft trotz deaktiviertem Starttyp. ' +
      'Dieser widersprüchliche Ausgangszustand kann nicht sicher zurückgesetzt werden.';
    Exit;
  end;
end;

function StopExistingServiceForUpdate: String;
var
  ExitCode: Integer;
  QueryResult: Integer;
  ServiceObject: Variant;
  ServiceState: String;
begin
  Result := '';
  if not ServiceExistedBefore then
    Exit;
  QueryResult := QueryService(ServiceObject);
  if QueryResult <> ServicePresent then
  begin
    Result :=
      'Der vorhandene Dienstzustand hat sich seit der Vorprüfung geändert. ' +
      'Die Installation wird vor jeder Dienständerung abgebrochen.';
    Exit;
  end;
  if not ServiceBelongsToThisInstallation(ServiceObject) then
  begin
    Result :=
      'Der vorhandene Dienst gehört nicht mehr eindeutig zu dieser Installation. ' +
      'Die Installation wird vor jeder Dienständerung abgebrochen.';
    Exit;
  end;
  ServiceState := String(ServiceObject.State);
  if not ServiceStateIsSupported(ServiceState) then
  begin
    Result :=
      'Der vorhandene Dienst meldet unmittelbar vor dem Update keinen stabilen Zustand RUNNING oder STOPPED. ' +
      'Die Installation wird vor jeder Dienständerung abgebrochen.';
    Exit;
  end;
  ServiceWasRunning := CompareText(ServiceState, 'Running') = 0;
  if not Sc('config "' + ServiceName + '" start= disabled', 'Dienst für das Update deaktivieren') then
  begin
    Result := 'Der vorhandene Dienst konnte vor dem Update nicht deaktiviert werden.';
    Exit;
  end;
  ServiceTemporarilyDisabled := True;
  if ServiceWasRunning then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop "' + ServiceName + '"', '', SW_HIDE,
      ewWaitUntilTerminated, ExitCode);
    if not WaitForServiceState('Stopped', ServiceWaitMilliseconds) then
    begin
      Result :=
        'Der vorhandene Dienst wurde nicht innerhalb der begrenzten Wartezeit gestoppt. ' +
        'Die geschützte Installations-Transaktion übernimmt die Recovery.';
      Exit;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not AcquireSetupUninstallMutex then
  begin
    Result :=
      'Eine andere Installation oder Deinstallation ist aktiv ' +
      'oder die systemweite Vorgangssperre konnte nicht sicher erworben werden. ' +
      'Es wurden keine Produktänderungen begonnen.';
    Exit;
  end;
  if ServicePrepared then
  begin
    Exit;
  end;
  ExtractTemporaryFile('{#OpenClientExeName}');
  if not ExecChecked(
    InternalOpenClient,
    '--assert-no-desktop-installation',
    'Desktop-Gegenmodus profilübergreifend und read-only ausschließen') then
  begin
    Result :=
      'Eine vorhandene oder unvollständig entfernte Desktop-Version wurde gefunden ' +
      'oder ein Benutzerprofil konnte nicht sicher geprüft werden. ' +
      'Deinstallieren Sie den E-Rechnungs-Prüfer Desktop unter "Installierte Apps" ' +
      'vollständig und starten Sie dieses Dienst-Setup anschließend erneut. ' +
      'Ein Desktop-API-Token wird nicht übernommen.';
    Exit;
  end;
  if not ExecChecked(
    InternalOpenClient,
    '--assert-no-pending-service-uninstall --expected-service-exe "' +
    ServiceLiveDir + '\{#ServiceExeName}"',
    'Offenen Deinstallationszustand vor Installations-Recovery ausschließen') then
  begin
    Result :=
      'Eine frühere Deinstallation ist noch nicht sicher abgeschlossen. ' +
      'Führen Sie denselben Deinstaller erneut aus, bevor Sie installieren oder aktualisieren.';
    Exit;
  end;
  Result := ReconcilePendingInstall;
  if Result <> '' then
    Exit;
  Result := InspectExistingService;
  if Result <> '' then
    Exit;
  if not ExecChecked(
    InternalOpenClient, '--preflight-machine',
    'Vorhandene ProgramData-Verzeichnisrechte sichern und Zustand prüfen') then
  begin
    Result :=
      'Der vorhandene Maschinenzustand ist unvollständig, unsicher oder ungültig. ' +
      'Nach einer gegebenenfalls notwendigen Absicherung der Verzeichnisrechte ' +
      'wird die Installation ohne weitere Änderung abgebrochen.';
    Exit;
  end;
  { With no owned service, every backend owner is foreign. The Desktop
    installation itself was already excluded without changing it. }
  if (not ServiceWasRunning) and CheckForMutexes(BackendMutexName) then
  begin
    Result :=
      'Ein fremder Prozess hält die maschinenweite Backend-Sperre. ' +
      'Die Installation wird ohne Dateiänderung abgebrochen.';
    Exit;
  end;
  if (not ServiceWasRunning) and not ExecChecked(
    InternalOpenClient, '--preflight-port',
    'Loopback-Port bei gestopptem oder fehlendem Dienst vorprüfen') then
  begin
    Result :=
      'Der konfigurierte lokale Dienstport ist belegt oder nicht exklusiv reservierbar. ' +
      'Die Installation wird vor dem Ersetzen von Dienstbinärdateien abgebrochen.';
    Exit;
  end;
  { Authoritative second check under the still-held cross-mode setup mutex,
    immediately before the installer creates persistent state. }
  if not ExecChecked(
    InternalOpenClient,
    '--assert-no-desktop-installation',
    'Desktop-Gegenmodus unmittelbar vor der Diensttransition erneut ausschließen') then
  begin
    Result :=
      'Während der Vorprüfung wurde eine Desktop-Installation erkannt oder ein ' +
      'Benutzerprofil konnte nicht sicher geprüft werden. ' +
      'Die Installation wurde vor der Diensttransition abgebrochen.';
    Exit;
  end;
  if not ForceDirectories(ExpandConstant('{app}')) then
  begin
    Result :=
      'Das feste Installationsverzeichnis für den geschützten ' +
      'Transaktionsbeleg konnte nicht angelegt werden.';
    Exit;
  end;
  if not BeginServiceTransition then
  begin
    Result :=
      'Die unveränderliche Dienst-Baseline konnte vor der ersten ' +
      'Dienständerung nicht persistent gespeichert werden.';
    if not RollbackPreparedInstall then
      Result := Result + ' Der geschützte Recovery-Zustand bleibt für den nächsten Setup-Lauf erhalten.';
    Exit;
  end;
  Result := StopExistingServiceForUpdate;
  if Result <> '' then
  begin
    if not RollbackPreparedInstall then
      Result := Result + ' Der geschützte Recovery-Zustand bleibt für den nächsten Setup-Lauf erhalten.';
    Exit;
  end;
  { Mandatory TOCTOU recheck after the owned service has stopped and before
    Inno Setup may create service.new or touch machine state. }
  if CheckForMutexes(BackendMutexName) then
  begin
    Result :=
      'Nach dem kontrollierten Dienststopp blieb die maschinenweite ' +
      'Backend-Sperre belegt.';
    if not RollbackPreparedInstall then
      Result := Result + ' Der geschützte Recovery-Zustand bleibt für den nächsten Setup-Lauf erhalten.';
    Exit;
  end;
  if not ExecChecked(
    InternalOpenClient, '--preflight-port',
    'Loopback-Port nach kontrolliertem Dienststopp erneut prüfen') then
  begin
    Result :=
      'Der konfigurierte lokale Dienstport ist nach dem kontrollierten ' +
      'Dienststopp belegt oder nicht exklusiv reservierbar.';
    if not RollbackPreparedInstall then
      Result := Result + ' Der geschützte Recovery-Zustand bleibt für den nächsten Setup-Lauf erhalten.';
    Exit;
  end;
  Result := PrepareServiceBundleTransaction;
  if Result <> '' then
  begin
    if not RollbackPreparedInstall then
      Result := Result + ' Der geschützte Recovery-Zustand bleibt für den nächsten Setup-Lauf erhalten.';
    Exit;
  end;
  ServicePrepared := True;
end;

{ Inno Setup catches ordinary exceptions from AfterInstall callbacks. Preserve
  the failure here and let the immediately following [Files] entry enter the
  transactional ProcessFileEntry path. }
procedure RecordServiceConfigurationFailure(Reason: String);
begin
  ServiceConfigurationFailed := True;
  ServiceConfigurationFailureReason := Reason;
  if ServiceConfigurationFailureReason = '' then
    ServiceConfigurationFailureReason :=
      'Die Dienstkonfiguration wurde nicht vollständig abgeschlossen.';
  Log('Dienstkonfiguration fehlgeschlagen: ' + ServiceConfigurationFailureReason);
end;

function ServiceConfigurationHasFailed: Boolean;
begin
  Result := ServiceConfigurationFailed;
end;

function PropagateServiceConfigurationFailure(Param: String): String;
begin
  Result := '';
  try
    Log('Dienstkonfigurationsfehler wird an den transaktionalen Installationspfad weitergegeben.');
    SuppressibleMsgBox(
      ServiceConfigurationFailureReason, mbCriticalError, MB_OK, IDOK);
  finally
    { ProcessFileEntry always rethrows EAbort, so an interactive installation
      cannot continue through Retry/Ignore after a configuration failure. }
    Abort;
  end;
end;

#ifdef AllowElevatedRecoveryTestContext
function TransactionalInstallFailureTestRequested: Boolean;
begin
  Result :=
    (CompareText(ExpandConstant('{param:ALLOWELEVATEDTESTCONTEXT|0}'), '1') = 0) and
    (CompareText(ExpandConstant('{param:TESTFAILAFTERCONFIG|0}'), '1') = 0);
end;
#endif

procedure ConfigureInstalledService;
var
  ServiceExe: String;
begin
  try
    ActivateStagedServiceBundle;
    ServiceExe := ExpandConstant('{app}\service\{#ServiceExeName}');
    if not ServiceExistedBefore then
    begin
      if not Sc(
        'create "' + ServiceName + '" binPath= "\"' + ServiceExe + '\"" ' +
        'start= disabled obj= "NT AUTHORITY\LocalService" DisplayName= "E-Rechnungs-Prüfer Dienst"',
        'Windows-Dienst anlegen') then
        RaiseException('Der Windows-Dienst konnte nicht angelegt werden.');
      ServiceCreatedBySetup := True;
    end
    else if not Sc(
      'config "' + ServiceName + '" binPath= "\"' + ServiceExe + '\"" ' +
      'obj= "NT AUTHORITY\LocalService"', 'Dienstpfad und Dienstkonto aktualisieren') then
      RaiseException('Der vorhandene Windows-Dienst konnte nicht aktualisiert werden.');

    if not Sc('sidtype "' + ServiceName + '" unrestricted', 'Dienstspezifischen SID aktivieren') then
      RaiseException('Der dienstspezifische Windows-SID konnte nicht aktiviert werden.');
    if not Sc(
      'description "' + ServiceName +
      '" "Lokaler Prüf- und Berichtsdienst für strukturierte elektronische Rechnungen."',
      'Dienstbeschreibung konfigurieren') then
      RaiseException('Die Dienstbeschreibung konnte nicht konfiguriert werden.');

    if not ExecChecked(
      ServiceExe, '--initialize',
      'Maschinenkonfiguration und Token initialisieren') then
      RaiseException('Maschinenkonfiguration oder Token konnten nicht sicher initialisiert werden.');
    if not ExecChecked(ServiceExe, '--verify-state', 'ProgramData-DACLs verifizieren') then
      RaiseException('Die ProgramData-DACLs konnten nicht verifiziert werden.');
    if not ExecChecked(ServiceExe, '--preflight-port', 'Konfiguration und lokalen Port prüfen') then
      RaiseException('Die Dienstkonfiguration ist ungültig oder ihr lokaler Port ist bereits belegt.');

    if not Sc(
      'failure "' + ServiceName + '" reset= 86400 actions= restart/60000/restart/300000/""/0',
      'Dienstwiederherstellung konfigurieren') then
      RaiseException('Die Dienstwiederherstellung konnte nicht konfiguriert werden.');
    if not Sc('failureflag "' + ServiceName + '" 1', 'Dienstfehleraktionen aktivieren') then
      RaiseException('Die Dienstfehleraktionen konnten nicht aktiviert werden.');

    if WizardIsTaskSelected('systemstart') then
    begin
      if not Sc('config "' + ServiceName + '" start= delayed-auto', 'Verzögerten Systemstart aktivieren') then
        RaiseException('Der verzögerte Systemstart konnte nicht aktiviert werden.');
    end
    else
    begin
      if not Sc('config "' + ServiceName + '" start= demand', 'Manuellen Dienststart aktivieren') then
        RaiseException('Der manuelle Dienststart konnte nicht aktiviert werden.');
      if not ExecChecked(
        InternalOpenClient,
        '--disable-service-delayed-start --expected-service-exe "' + ServiceExe + '"',
        'Verzögerten Dienststart über den SCM deaktivieren') then
        RaiseException('Der verzögerte Dienststart konnte nicht über den SCM deaktiviert werden.');
    end;

    #ifdef AllowElevatedRecoveryTestContext
    if TransactionalInstallFailureTestRequested then
    begin
      RecordServiceConfigurationFailure(
        'Absichtlich ausgelöster transaktionaler Installationstest.');
      Exit;
    end;
    #endif

    if (not ServiceExistedBefore) or ServiceWasRunning then
    begin
      if not Sc('start "' + ServiceName + '"', 'Dienst starten') or
         not WaitForServiceState('Running', ServiceWaitMilliseconds) then
        RaiseException('Der installierte Dienst wurde nicht betriebsbereit.');
      if not ExecChecked(ServiceExe, '--health-check', 'Loopback-Healthcheck des Dienstes prüfen') then
        RaiseException('Der Dienst meldet trotz SCM-Start keine betriebsbereite lokale API.');
    end;
  except
    RecordServiceConfigurationFailure(GetExceptionMessage);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then
  begin
    if not CommitServiceBundle then
      RaiseException(
        'Der Dienststand konnte nicht atomar abgeschlossen werden; die Installation wird zurückgenommen.');
    if not MarkServiceCommitted then
      RaiseException(
        'Der neue Dienststand konnte nicht vollständig als Commit bewiesen werden; ' +
        'die Installation bleibt rollbackfähig.');
    TransactionCommitStarted := True;
    FinalizeServiceBundle;
    if not FinishTerminalInstallTransaction then
      RaiseException(
        'Der Dienst-Commit ist abgeschlossen; der persistente ' +
        'Transaktionsbeleg konnte noch nicht finalisiert werden.');
    ServiceTemporarilyDisabled := False;
    ServiceCreatedBySetup := False;
    InstallSucceeded := True;
  end;
end;

procedure CancelInitialWizardActivationTimer;
begin
  if InitialWizardActivationTimer <> 0 then
  begin
    KillTimer(0, InitialWizardActivationTimer);
    InitialWizardActivationTimer := 0;
  end;
end;

procedure CancelInitialWizardFallbackCleanupTimer;
begin
  if InitialWizardFallbackCleanupTimer <> 0 then
  begin
    KillTimer(0, InitialWizardFallbackCleanupTimer);
    InitialWizardFallbackCleanupTimer := 0;
  end;
end;

procedure RemoveInitialWizardTopMost;
begin
  CancelInitialWizardFallbackCleanupTimer;
  if not InitialWizardFallbackTopMost then
    Exit;
  InitialWizardFallbackTopMost := False;
  if not SetWindowPos(
    WizardForm.Handle, SetupHwndNotTopMost, 0, 0, 0, 0,
    SetupSwpNoMove or SetupSwpNoSize or SetupSwpNoActivate or
    SetupSwpShowWindow) then
    Log(
      'Der vorübergehende Sichtbarkeitshinweis des Setupfensters ' +
      'konnte nicht zurückgesetzt werden.');
end;

procedure InitialWizardFallbackCleanupTimerProcedure(
  Window: HWND; MessageCode: UINT; TimerID: UINT_PTR; TickCount: DWORD);
begin
  CancelInitialWizardFallbackCleanupTimer;
  RemoveInitialWizardTopMost;
end;

procedure ScheduleInitialWizardFallbackCleanup;
begin
  CancelInitialWizardFallbackCleanupTimer;
  InitialWizardFallbackCleanupTimer := SetTimer(
    0, 0, InitialWizardFallbackCleanupMilliseconds,
    CreateCallback(@InitialWizardFallbackCleanupTimerProcedure));
  if InitialWizardFallbackCleanupTimer = 0 then
  begin
    Log(
      'Die zeitliche Begrenzung des Sichtbarkeitshinweises konnte nicht ' +
      'geplant werden; der Hinweis wird sofort zurückgesetzt.');
    RemoveInitialWizardTopMost;
  end;
end;

procedure InitialWizardActivated(Sender: TObject);
begin
  RemoveInitialWizardTopMost;
end;

procedure ActivateInitialWizard;
begin
  { Do not consume the single activation attempt before the top-level form is
    actually visible or while Setup is already being torn down. }
  if InitialWizardActivationShuttingDown or
     InitialWizardActivationCompleted or WizardSilent or
     (not WizardForm.Visible) then
    Exit;
  InitialWizardActivationCompleted := True;
  ShowWindow(WizardForm.Handle, SetupSwRestore);
  BringToFrontAndRestore;
  if GetForegroundWindow = WizardForm.Handle then
    Exit;

  { Windows may reject foreground activation after the UAC desktop switch.
    In that case, make Setup visible without synthesizing input or stealing
    focus. The temporary topmost state ends on activation, after ten seconds,
    or during teardown, whichever comes first. }
  if SetWindowPos(
    WizardForm.Handle, SetupHwndTopMost, 0, 0, 0, 0,
    SetupSwpNoMove or SetupSwpNoSize or SetupSwpNoActivate or
    SetupSwpShowWindow) then
  begin
    InitialWizardFallbackTopMost := True;
    ScheduleInitialWizardFallbackCleanup;
  end
  else
    Log('Das Setupfenster konnte nicht sichtbar hervorgehoben werden.');

  if not SetForegroundWindow(WizardForm.Handle) then
    Log('Windows hat die einmalige Vordergrundaktivierung des Setupfensters abgelehnt.');
  if GetForegroundWindow = WizardForm.Handle then
    RemoveInitialWizardTopMost;
end;

procedure InitialWizardActivationTimerProcedure(
  Window: HWND; MessageCode: UINT; TimerID: UINT_PTR; TickCount: DWORD);
begin
  if InitialWizardActivationShuttingDown then
    Exit;
  if WizardSilent or InitialWizardActivationCompleted then
  begin
    CancelInitialWizardActivationTimer;
    Exit;
  end;
  if not WizardForm.Visible then
    Exit;
  CancelInitialWizardActivationTimer;
  ActivateInitialWizard;
end;

procedure ScheduleInitialWizardActivation(Sender: TObject);
begin
  if WizardSilent or InitialWizardActivationScheduled then
    Exit;
  InitialWizardActivationScheduled := True;
  InitialWizardActivationTimer := SetTimer(
    0, 0, 50, CreateCallback(@InitialWizardActivationTimerProcedure));
  if InitialWizardActivationTimer = 0 then
  begin
    Log('Die verzögerte Aktivierung des Setupfensters konnte nicht geplant werden.');
    ActivateInitialWizard;
  end;
end;

procedure InitializeWizard;
begin
  if not WizardSilent then
  begin
    WizardForm.OnActivate := @InitialWizardActivated;
    WizardForm.OnShow := @ScheduleInitialWizardActivation;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  { CurStepChanged exceptions are reported but do not replace Inno's success
    exit code. Make every prepared but unfinished transaction non-successful. }
  if ServicePrepared and not InstallSucceeded then
    Result := 4;
end;

procedure DeinitializeSetup;
begin
  InitialWizardActivationShuttingDown := True;
  CancelInitialWizardActivationTimer;
  RemoveInitialWizardTopMost;
  try
    if InstallSucceeded then
      Exit;
    // Cancellation before PrepareToInstall has not initialized the application
    // directory and owns no transaction state that could require rollback.
    if not SetupUninstallMutexOwned then
      Exit;

    { The persistent commit marker is the no-return boundary. Never invoke a
      rollback path after it has been proven and written through. }
    if TransactionCommitStarted then
    begin
      try
        FinalizeServiceBundle;
        if not FinishTerminalInstallTransaction then
          Log('Der committed Installationszustand bleibt für den nächsten Setup-Lauf erhalten.');
      except
        Log(
          'Die Roll-forward-Recovery nach der Commit-Grenze wurde unterbrochen: ' +
          GetExceptionMessage);
      end;
      Exit;
    end;

    if ServiceTransactionPrepared and not RollbackServiceConfiguration then
      Exit;
    if ServiceTransactionPrepared and not FinishTerminalInstallTransaction then
      Exit;
    RemoveEmptyInstallRootAfterRollback;
  finally
    ReleaseSetupUninstallMutex;
  end;
end;

function InitializeUninstall: Boolean;
var
  ReconcileDirection: Integer;
  ServiceObject: Variant;
  QueryResult: Integer;
  ServiceState: String;
begin
  Result := False;
  if not AcquireSetupUninstallMutex then
  begin
    Log(
      'Die Deinstallation wird abgebrochen, weil eine andere Installation ' +
      'oder Deinstallation aktiv ist oder die systemweite Vorgangssperre ' +
      'nicht sicher erworben werden konnte.');
    if not UninstallSilent then
      MsgBox(
        'Eine andere Installation oder Deinstallation ist aktiv. ' +
        'Versuchen Sie es nach deren Abschluss erneut.',
        mbError, MB_OK);
    Exit;
  end;
  Result := True;
  if not ClassifyInstallReconcile(ReconcileDirection) or
     (ReconcileDirection <> ReconcileNone) then
  begin
    Log(
      'Die Deinstallation wird abgebrochen, weil zuerst eine persistente ' +
      'Installations-Recovery abgeschlossen werden muss.');
    if not UninstallSilent then
      MsgBox(
        'Eine frühere Installation ist noch nicht transaktional abgeschlossen. ' +
        'Führen Sie denselben Dienst-Installer zuerst erneut aus; die Deinstallation ' +
        'verändert bis dahin weder Dienst noch Recovery-Belege.',
        mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if not ReconcileInterruptedServiceUninstall then
  begin
    Log(
      'Die Deinstallation wird abgebrochen, weil ein früherer ' +
      'Deinstallationsbeleg nicht sicher reconciliert werden konnte.');
    if not UninstallSilent then
      MsgBox(
        'Eine frühere Deinstallation konnte nicht sicher zurückgesetzt oder abgeschlossen werden. ' +
        'Dienst und geschützter Beleg bleiben unverändert.',
        mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if UninstallSilent then
    PurgeMachineData := CompareText(ExpandConstant('{param:PURGEDATA|0}'), '1') = 0
  else
    PurgeMachineData :=
      MsgBox(
        'Sollen Maschinenkonfiguration, API-Token und technische Protokolle endgültig gelöscht werden?'#13#10#13#10 +
        '„Nein“ behält diese Daten für eine spätere Neuinstallation bei.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  QueryResult := QueryService(ServiceObject);
  if QueryResult = ServiceQueryError then
  begin
    Log('Die Deinstallation wird abgebrochen, weil der SCM-Zustand nicht sicher ermittelt werden konnte.');
    if not UninstallSilent then
      MsgBox('Der Windows-Dienststatus konnte nicht sicher ermittelt werden.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if QueryResult = ServiceAbsent then
    Exit;
  if not ServiceBelongsToThisInstallation(ServiceObject) then
  begin
    Log('Die Deinstallation wird abgebrochen, weil der gleichnamige Dienst nicht mehr eindeutig zum Produkt gehört.');
    if not UninstallSilent then
      MsgBox(
        'Ein gleichnamiger fremder Windows-Dienst wird aus Sicherheitsgründen nicht verändert.',
        mbError, MB_OK);
    Result := False;
    Exit;
  end;
  ServiceState := String(ServiceObject.State);
  if not ServiceStateIsSupported(ServiceState) then
  begin
    Log('Die Deinstallation wird wegen eines nicht stabilen Dienstzustands abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der vorhandene Dienst ist weder stabil gestartet noch vollständig gestoppt.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  ServiceExistedBefore := True;
  ServiceWasRunning := CompareText(ServiceState, 'Running') = 0;
  OriginalStartMode := String(ServiceObject.StartMode);
  if not ServiceStartModeIsSupported(OriginalStartMode) then
  begin
    Log('Die Deinstallation wird wegen eines unbekannten Dienststarttyps abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der Starttyp des vorhandenen Dienstes konnte nicht sicher eingeordnet werden.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode) then
  begin
    Log('Die Deinstallation wird wegen des widersprüchlichen Zustands RUNNING plus DISABLED abgebrochen.');
    if not UninstallSilent then
      MsgBox(
        'Der vorhandene Dienst läuft trotz deaktiviertem Starttyp und kann nicht sicher zurückgesetzt werden.',
        mbError, MB_OK);
    Result := False;
    Exit;
  end;
  UninstallStateValidated := True;
end;

procedure RemoveServiceForConfirmedUninstall;
var
  ServiceObject: Variant;
  ExitCode: Integer;
  QueryResult: Integer;
  ServiceState: String;
begin
  if not UninstallStateValidated or not ServiceExistedBefore then
    Exit;
  QueryResult := QueryService(ServiceObject);
  if QueryResult = ServiceAbsent then
  begin
    ServiceExistedBefore := False;
    Exit;
  end;
  if QueryResult = ServiceQueryError then
  begin
    Log('Die bestätigte Deinstallation wird wegen eines nicht mehr lesbaren Dienstzustands abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der Windows-Dienstzustand hat sich geändert und wird nicht verändert.', mbError, MB_OK);
    Abort;
  end;
  if not ServiceBelongsToThisInstallation(ServiceObject) then
  begin
    Log('Die bestätigte Deinstallation wird wegen eines nicht mehr eindeutig zugehörigen Dienstzustands abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der Windows-Dienst gehört nicht mehr eindeutig zu dieser Installation.', mbError, MB_OK);
    Abort;
  end;
  ServiceState := String(ServiceObject.State);
  if not ServiceStateIsSupported(ServiceState) then
  begin
    Log('Die bestätigte Deinstallation wird wegen eines nicht stabilen Dienstzustands abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der Windows-Dienst ist weder stabil gestartet noch vollständig gestoppt.', mbError, MB_OK);
    Abort;
  end;
  ServiceWasRunning := CompareText(ServiceState, 'Running') = 0;
  OriginalStartMode := String(ServiceObject.StartMode);
  if not ServiceStartModeIsSupported(OriginalStartMode) then
  begin
    Log('Die bestätigte Deinstallation wird wegen eines unbekannten Dienststarttyps abgebrochen.');
    if not UninstallSilent then
      MsgBox('Der Starttyp des vorhandenen Dienstes konnte nicht sicher eingeordnet werden.', mbError, MB_OK);
    Abort;
  end;
  if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode) then
  begin
    Log('Die bestätigte Deinstallation wird wegen des widersprüchlichen Zustands RUNNING plus DISABLED abgebrochen.');
    if not UninstallSilent then
      MsgBox(
        'Der Windows-Dienst läuft trotz deaktiviertem Starttyp und wird nicht verändert.',
        mbError, MB_OK);
    Abort;
  end;
  if not CaptureOriginalServiceMetadata then
  begin
    Log('Die bestätigte Deinstallation wird wegen unlesbarer SCM-Metadaten abgebrochen.');
    if not UninstallSilent then
      MsgBox('Die vorhandenen Dienstmetadaten konnten nicht sicher gelesen werden.', mbError, MB_OK);
    Abort;
  end;
  ServiceTemporarilyDisabled := True;
  if not Sc('config "' + ServiceName + '" start= disabled', 'Dienst vor Deinstallation deaktivieren') then
  begin
    Log('Der geschützte Deinstallationsbeleg bleibt für den nächsten Lauf erhalten.');
    Abort;
  end;
  if ServiceWasRunning then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop "' + ServiceName + '"', '', SW_HIDE,
      ewWaitUntilTerminated, ExitCode);
    if not WaitForServiceState('Stopped', ServiceWaitMilliseconds) then
    begin
      Log(
        'Der Dienststopp ist nicht stabil abgeschlossen; der geschützte ' +
        'Deinstallationsbeleg bleibt für den nächsten Lauf erhalten.');
      if not UninstallSilent then
        MsgBox('Der Dienst konnte nicht kontrolliert gestoppt werden.', mbError, MB_OK);
      Abort;
    end;
  end;
  if not Sc('delete "' + ServiceName + '"', 'Dienst aus dem SCM entfernen') or
     not WaitForServiceRemoval(ServiceWaitMilliseconds) then
  begin
    Log(
      'Die Dienstlöschung ist nicht stabil abgeschlossen; der geschützte ' +
      'Deinstallationsbeleg bleibt für den nächsten Lauf erhalten.');
    if not UninstallSilent then
      MsgBox('Der Dienst konnte nicht vollständig aus dem SCM entfernt werden.', mbError, MB_OK);
    Abort;
  end;
  ServiceExistedBefore := False;
  ServiceTemporarilyDisabled := False;
end;

procedure RemoveOwnedServiceDirectories;
var
  Removed: Boolean;
begin
  Removed := DeleteTreeIfPresent(ServiceNewDir, 'Dienst-Staging');
  if not DeleteTreeIfPresent(ServiceRollbackDir, 'Dienst-Rollback') then
    Removed := False;
  if not DeleteTreeIfPresent(ServiceObsoleteDir, 'Abgelöster Dienststand') then
    Removed := False;
  if not DeleteTreeIfPresent(ServiceLiveDir, 'Installierter Dienststand') then
    Removed := False;
  if not Removed then
    RaiseException('Die Dienst-Binärdateien konnten nicht vollständig entfernt werden.');
end;

procedure PurgeOwnedMachineState;
begin
  if not ExecChecked(
    InternalOpenClient, '--purge-machine-state',
    'Geschützten Maschinenzustand erneut prüfen und löschen') then
    RaiseException(
      'Der ausgewählte Maschinenzustand war nicht sicher löschbar; ' +
      'die Dienst-Binärdateien bleiben für eine sichere Wiederholung erhalten.');
end;

procedure PurgeTransientRuntimeState;
begin
  if not ExecChecked(
    InternalOpenClient, '--purge-runtime-state',
    'Temporären KoSIT-Dienstzustand erneut prüfen und löschen') then
    RaiseException(
      'Der temporäre KoSIT-Dienstzustand war nicht sicher löschbar; ' +
      'die Dienst-Binärdateien bleiben für eine sichere Wiederholung erhalten.');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    RemoveServiceForConfirmedUninstall;
    if not ClearOriginalServiceMetadata then
      RaiseException(
        'Die geschützte SCM-Sicherung konnte nicht entfernt werden; ' +
        'die Dienst-Binärdateien bleiben für eine sichere Wiederholung erhalten.');
    PurgeTransientRuntimeState;
    if PurgeMachineData then
      PurgeOwnedMachineState;
    RemoveOwnedServiceDirectories;
  end;
end;

procedure DeinitializeUninstall;
begin
  try
    if ServiceMetadataCaptured and not ServiceTemporarilyDisabled then
      ClearOriginalServiceMetadata;
  finally
    ReleaseSetupUninstallMutex;
  end;
end;
