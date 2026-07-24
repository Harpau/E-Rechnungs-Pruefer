from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_desktop_installer_remains_a_separate_unprivileged_option() -> None:
    installer = _read("packaging/windows/installer.iss")

    assert "AppId={{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}" in installer
    assert r"DefaultDirName={localappdata}\Programs\E-Rechnungs-Pruefer" in installer
    assert "PrivilegesRequired=lowest" in installer
    assert 'Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in installer
    assert 'Name: "autostart"' in installer
    assert "RegKeyExists(HKLM64, 'SYSTEM\\CurrentControlSet\\Services\\ERechnungsPrueferService')" in installer


def test_service_installer_is_machine_wide_and_fail_closed() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    for expected in (
        "AppId={{8824D15C-7F4E-4CB2-B957-FBC26B923363}",
        r"DefaultDirName={autopf64}\E-Rechnungs-Pruefer-Dienst",
        "PrivilegesRequired=admin",
        'Name: "systemstart"',
        "Flags: checkedonce",
        'obj= "NT AUTHORITY\\LocalService"',
        'sidtype "' + "' + ServiceName + '" + '" unrestricted',
        "start= delayed-auto",
        "start= demand",
        'failure "' + "' + ServiceName + '" + '" reset= 86400',
        'failureflag "' + "' + ServiceName + '" + '" 1',
        "PrepareToInstall",
        "WaitForServiceState('Stopped'",
        "WaitForServiceRemoval",
        "ServiceBelongsToThisInstallation",
        "ServiceStartModeIsSupported",
        "ServiceStateIsSupported",
        "CaptureOriginalServiceMetadata: Boolean",
        "--snapshot-service-metadata",
        "--reconcile-service-uninstall",
        "--assert-no-pending-service-uninstall",
        "--preflight-machine",
        "--preflight-port",
        "--verify-migration-context",
        "--commit-desktop-migration",
        "--clear-desktop-migration-seal",
        r'DestDir: "{app}\service.new"',
        r'Name: "{group}\E-Rechnungs-Prüfer öffnen"',
        r'Filename: "{app}\service\{#OpenClientExeName}"',
        "uninsneveruninstall",
        "PrepareServiceBundleTransaction",
        "ActivateStagedServiceBundle",
        "CommitServiceBundle",
        "FinalizeServiceBundle",
        "ServiceObsoleteDir",
        "RemoveServiceForConfirmedUninstall",
        "TESTFAILAFTERCONFIG",
        "--consent-token-import",
        "--verify-state",
        "PurgeMachineData",
        "MB_DEFBUTTON2",
        "PurgeOwnedMachineState",
        "PurgeTransientRuntimeState",
        "--purge-runtime-state",
        "--purge-machine-state",
        "RemoveOwnedServiceDirectories",
        "#ifdef AllowElevatedMigrationTestContext",
        "ALLOWELEVATEDTESTCONTEXT",
        "--disable-service-delayed-start",
    ):
        assert expected in installer

    assert "LocalSystem" not in installer
    assert "{commongroup}" not in installer
    assert "RegQueryBinaryValue" not in installer
    assert "RegWriteBinaryValue" not in installer
    assert "RegDeleteValue" not in installer
    assert "for Item in Services do" not in installer
    assert "ServiceObject := Services.ItemIndex(0);" in installer
    assert "TokenMigrationPage.Selected[" not in installer
    assert installer.count("TokenMigrationPage.Values[0]") == 7
    assert "[UninstallDelete]" not in installer
    assert 'Source: "{#OpenClientFile}"; DestDir: "{app}"' not in installer
    assert (
        'Source: "{#OpenClientFile}"; DestDir: "{app}\\service.new"; Flags: ignoreversion uninsneveruninstall'
    ) in installer
    assert "--service-snapshot" not in installer
    assert r"{tmp}\service-metadata" not in installer
    assert "--clear-service-metadata" in installer
    assert installer.count("ALLOWELEVATEDTESTCONTEXT") == 1
    assert installer.count("ServiceBelongsToThisInstallation(ServiceObject)") >= 3
    assert "CompareText(String(ServiceObject.State), 'Stopped') <> 0" not in installer
    assert installer.count("ServiceWasRunning := CompareText(ServiceState, 'Running') = 0;") == 4
    state_validation = installer[
        installer.index("function ServiceStateIsSupported") : installer.index("function WaitForServiceState")
    ]
    assert "CompareText(ServiceState, 'Running') = 0" in state_validation
    assert "CompareText(ServiceState, 'Stopped') = 0" in state_validation
    rollbackable_baseline = installer[
        installer.index("function ServiceBaselineIsRollbackable") : installer.index("function ServiceStateIsSupported")
    ]
    assert "CompareText(ServiceState, 'Running') = 0" in rollbackable_baseline
    assert "CompareText(StartMode, 'Disabled') = 0" in rollbackable_baseline
    service_inspection = installer[
        installer.index("function InspectExistingService") : installer.index("function StopExistingServiceForUpdate")
    ]
    assert "if not ServiceStateIsSupported(ServiceState)" in service_inspection
    assert "if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode)" in service_inspection
    assert "CaptureOriginalServiceMetadata" not in service_inspection
    install_flow = installer[
        installer.index("function InspectExistingService") : installer.index("procedure InitializeWizard")
    ]
    assert "CaptureOriginalServiceMetadata" not in install_flow
    assert "--snapshot-service-metadata" not in install_flow
    update_stop = installer[
        installer.index("function StopExistingServiceForUpdate") : installer.index("function PrepareToInstall")
    ]
    assert update_stop.index("QueryResult := QueryService(ServiceObject)") < update_stop.index(
        "if not ServiceStateIsSupported(ServiceState)"
    )
    assert update_stop.index("if not ServiceStateIsSupported(ServiceState)") < update_stop.index("if not Sc('config")
    assert installer.index("procedure ConfigureInstalledService") < installer.index("procedure DeinitializeSetup")
    done_step = installer[installer.index("procedure CurStepChanged") : installer.index("procedure InitializeWizard")]
    assert done_step.index("CommitDesktopMigration;") < done_step.index("InstallSucceeded := True;")
    assert done_step.index("ClearDesktopMigrationSeal") < done_step.index("InstallSucceeded := True;")
    seal_cleanup = installer[
        installer.index("function ClearDesktopMigrationSeal") : installer.index("function VerifyDesktopMigrationOwner")
    ]
    assert "ExecChecked(" in seal_cleanup
    assert "InternalOpenClient" in seal_cleanup
    assert "ExecAsOriginalUser(" not in seal_cleanup
    prepare_migration = installer[
        installer.index("function PrepareDesktopMigration") : installer.index("function BeginServiceTransition")
    ]
    assert (
        prepare_migration.index("--plan-desktop-migration")
        < prepare_migration.index("--seal-desktop-migration")
        < prepare_migration.index("MigrationPrepared := True;")
        < prepare_migration.index("--apply-desktop-migration")
        < prepare_migration.index("--verify-applied-desktop-migration")
    )
    clear_metadata = installer[
        installer.index("function ClearOriginalServiceMetadata") : installer.index("function Sc(")
    ]
    assert "if not ServiceMetadataCaptured" not in clear_metadata
    uninstall_step = installer[
        installer.index("procedure CurUninstallStepChanged") : installer.index("procedure DeinitializeUninstall")
    ]
    assert "CurUninstallStep = usUninstall" in uninstall_step
    assert "RemoveOwnedServiceDirectories;" in uninstall_step
    assert "PurgeOwnedMachineState;" in uninstall_step
    assert "PurgeTransientRuntimeState;" in uninstall_step
    assert "if not ClearOriginalServiceMetadata then" in uninstall_step
    assert "RaiseException(" in uninstall_step
    assert (
        uninstall_step.index("RemoveServiceForConfirmedUninstall;")
        < uninstall_step.index("if not ClearOriginalServiceMetadata then")
        < uninstall_step.index("PurgeTransientRuntimeState;")
        < uninstall_step.index("RemoveOwnedServiceDirectories;")
    )
    assert "usPostUninstall" not in uninstall_step
    assert uninstall_step.index("PurgeOwnedMachineState;") < uninstall_step.index("RemoveOwnedServiceDirectories;")
    assert uninstall_step.index("PurgeTransientRuntimeState;") < uninstall_step.index("RemoveOwnedServiceDirectories;")
    assert uninstall_step.index("PurgeTransientRuntimeState;") < uninstall_step.index("if PurgeMachineData then")
    initialize_uninstall = installer[
        installer.index("function InitializeUninstall") : installer.index(
            "procedure RemoveServiceForConfirmedUninstall"
        )
    ]
    assert "Sc(" not in initialize_uninstall
    assert "ClassifyInstallReconcile(ReconcileDirection)" in initialize_uninstall
    assert "ReconcileDirection <> ReconcileNone" in initialize_uninstall
    assert initialize_uninstall.index("ClassifyInstallReconcile") < initialize_uninstall.index(
        "ReconcileInterruptedServiceUninstall"
    )
    assert initialize_uninstall.index("ReconcileInterruptedServiceUninstall") < initialize_uninstall.index(
        "QueryResult := QueryService"
    )
    assert "FinishInstallReconcile" not in initialize_uninstall
    assert "if not ServiceStateIsSupported(ServiceState)" in initialize_uninstall
    assert "if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode)" in initialize_uninstall
    remove_for_uninstall = installer[
        installer.index("procedure RemoveServiceForConfirmedUninstall") : installer.index(
            "procedure RemoveOwnedServiceDirectories"
        )
    ]
    assert remove_for_uninstall.index("if not ServiceStateIsSupported(ServiceState)") < remove_for_uninstall.index(
        "CaptureOriginalServiceMetadata"
    )
    assert remove_for_uninstall.index(
        "if not ServiceBaselineIsRollbackable(ServiceState, OriginalStartMode)"
    ) < remove_for_uninstall.index("CaptureOriginalServiceMetadata")
    assert remove_for_uninstall.index("ServiceTemporarilyDisabled := True;") < remove_for_uninstall.index(
        "if not Sc('config"
    )
    assert "Dienst nach fehlgeschlagener Deinstallation wieder starten" not in remove_for_uninstall
    assert "RestoreOriginalServiceStart" not in installer
    assert remove_for_uninstall.count("Deinstallationsbeleg bleibt für den nächsten Lauf erhalten") == 3
    assert remove_for_uninstall.index("if not ServiceStateIsSupported(ServiceState)") < remove_for_uninstall.index(
        "if not Sc('config"
    )
    rollback = installer[
        installer.index("function RollbackServiceConfiguration") : installer.index("function ClassifyInstallReconcile")
    ]
    assert "--mark-service-rollback-complete" in rollback
    assert "Sc(" not in rollback
    assert "DelTree(" not in rollback
    assert "--purge-machine-state" not in rollback
    preflight = installer[
        installer.index("function PrepareToInstall") : installer.index("procedure ConfigureInstalledService")
    ]
    assert (
        preflight.index("ExtractTemporaryFile")
        < preflight.index("--assert-no-pending-service-uninstall")
        < preflight.index("ReconcilePendingInstall")
    )
    assert preflight.index("ReconcilePendingInstall") < preflight.index("--preflight-machine")
    assert "(not ServiceWasRunning) and CheckForMutexes(BackendMutexName)" in preflight
    assert "(not ServiceWasRunning) and not ExecChecked(" in preflight
    assert preflight.count("CheckForMutexes(BackendMutexName)") == 2
    assert preflight.count("--preflight-port") == 2
    post_stop = preflight[
        preflight.index("Result := StopExistingServiceForUpdate;") : preflight.index("ServicePrepared := True;")
    ]
    assert "if CheckForMutexes(BackendMutexName) then" in post_stop
    assert "--preflight-port" in post_stop
    assert "ServiceWasRunning" not in post_stop
    assert (
        preflight.index("(not ServiceWasRunning) and CheckForMutexes(BackendMutexName)")
        < preflight.index("Loopback-Port bei gestopptem oder fehlendem Dienst vorprüfen")
        < preflight.index("PrepareDesktopMigration")
        < preflight.index("ForceDirectories(ExpandConstant('{app}'))")
        < preflight.index("BeginServiceTransition")
        < preflight.index("StopExistingServiceForUpdate")
        < preflight.rindex("CheckForMutexes(BackendMutexName)")
        < preflight.rindex("--preflight-port")
        < preflight.index("PrepareServiceBundleTransaction")
    )


def test_service_installer_serializes_setup_and_uninstall_mutations() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    assert "SetupUninstallMutexName = 'Global\\E-Rechnungs-Pruefer-Service-Setup-Uninstall';" in installer
    assert "SetupUninstallMutexName = BackendMutexName" not in installer
    for expected in (
        "function CreateMutexW(",
        "function WaitForSingleObject(",
        "function ReleaseMutex(",
        "function CloseHandle(",
        "function AcquireSetupUninstallMutex: Boolean;",
        "procedure ReleaseSetupUninstallMutex;",
        "WaitObject0",
        "WaitAbandoned0",
        "WaitTimeout",
        "WaitFailed",
    ):
        assert expected in installer

    acquire = installer[
        installer.index("function AcquireSetupUninstallMutex") : installer.index("procedure ReleaseSetupUninstallMutex")
    ]
    assert acquire.index("CreateMutexW(") < acquire.index("WaitForSingleObject(")
    assert "(WaitResult = WaitObject0) or (WaitResult = WaitAbandoned0)" in acquire
    assert "SetupUninstallMutexOwned := True;" in acquire
    assert "if WaitResult = WaitFailed then" in acquire
    assert "DLLGetLastError" in acquire
    assert "CloseHandle(SetupUninstallMutexHandle)" in acquire
    assert "SetupUninstallMutexHandle := 0;" in acquire

    release = installer[
        installer.index("procedure ReleaseSetupUninstallMutex") : installer.index("function ServiceLiveDir")
    ]
    assert "if SetupUninstallMutexHandle = 0 then" in release
    assert "if SetupUninstallMutexOwned then" in release
    assert release.index("ReleaseMutex(SetupUninstallMutexHandle)") < release.index(
        "CloseHandle(SetupUninstallMutexHandle)"
    )
    assert "SetupUninstallMutexOwned := False;" in release
    assert "SetupUninstallMutexHandle := 0;" in release

    prepare = installer[
        installer.index("function PrepareToInstall") : installer.index("procedure ConfigureInstalledService")
    ]
    assert prepare.index("AcquireSetupUninstallMutex") < prepare.index("if ServicePrepared then")
    assert prepare.index("AcquireSetupUninstallMutex") < prepare.index("ExtractTemporaryFile")

    initialize_uninstall = installer[
        installer.index("function InitializeUninstall") : installer.index(
            "procedure RemoveServiceForConfirmedUninstall"
        )
    ]
    assert initialize_uninstall.index("AcquireSetupUninstallMutex") < initialize_uninstall.index(
        "ClassifyInstallReconcile"
    )

    deinitialize_setup = installer[
        installer.index("procedure DeinitializeSetup") : installer.index("function InitializeUninstall")
    ]
    assert "finally" in deinitialize_setup
    assert "ReleaseSetupUninstallMutex;" in deinitialize_setup
    assert deinitialize_setup.rindex("ReleaseSetupUninstallMutex;") > deinitialize_setup.rindex("finally")
    assert deinitialize_setup.index("if not SetupUninstallMutexOwned then") < deinitialize_setup.index(
        "if TransactionCommitStarted then"
    )
    assert deinitialize_setup.index("if not SetupUninstallMutexOwned then") < deinitialize_setup.index(
        "RemoveEmptyInstallRootAfterRollback;"
    )

    deinitialize_uninstall = installer[installer.index("procedure DeinitializeUninstall") :]
    assert "finally" in deinitialize_uninstall
    assert "ReleaseSetupUninstallMutex;" in deinitialize_uninstall
    assert deinitialize_uninstall.rindex("ReleaseSetupUninstallMutex;") > deinitialize_uninstall.rindex("finally")


def test_service_installer_activates_the_visible_wizard_once_after_show() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    assert "{ Cancellation before PrepareToInstall has not initialized {app}" not in installer
    assert "InitialWizardPageActivated" not in installer
    assert "procedure CurPageChanged" not in installer
    assert "InitialWizardFallbackCleanupMilliseconds = 10000;" in installer
    assert "function ShowWindow(Window: HWND; ShowCommand: Integer): BOOL;" in installer
    assert "function SetForegroundWindow(Window: HWND): BOOL;" in installer
    assert "function GetForegroundWindow: HWND;" in installer
    assert "function SetWindowPos(" in installer

    schedule = installer[
        installer.index("procedure ScheduleInitialWizardActivation") : installer.index("procedure InitializeWizard")
    ]
    assert "WizardSilent or InitialWizardActivationScheduled" in schedule
    assert schedule.count("SetTimer(") == 1
    assert schedule.count("CreateCallback(@InitialWizardActivationTimerProcedure)") == 1
    assert "TimerProcedure: LongWord" in installer
    assert "NativeInt" not in installer

    timer = installer[
        installer.index("procedure InitialWizardActivationTimerProcedure") : installer.index(
            "procedure ScheduleInitialWizardActivation"
        )
    ]
    visible_branch = timer[timer.index("if not WizardForm.Visible then") :]
    assert visible_branch.index("if not WizardForm.Visible then") < visible_branch.index(
        "CancelInitialWizardActivationTimer;"
    )
    assert visible_branch.index("CancelInitialWizardActivationTimer;") < visible_branch.index("ActivateInitialWizard;")

    activate = installer[
        installer.index("procedure ActivateInitialWizard") : installer.index(
            "procedure InitialWizardActivationTimerProcedure"
        )
    ]
    assert activate.index("(not WizardForm.Visible)") < activate.index("InitialWizardActivationCompleted := True;")
    assert activate.index("InitialWizardActivationCompleted := True;") < activate.index(
        "ShowWindow(WizardForm.Handle, SetupSwRestore);"
    )
    assert activate.index("ShowWindow(WizardForm.Handle, SetupSwRestore);") < activate.index("BringToFrontAndRestore;")
    assert activate.index("BringToFrontAndRestore;") < activate.index("if GetForegroundWindow = WizardForm.Handle then")
    assert activate.index("if GetForegroundWindow = WizardForm.Handle then") < activate.index(
        "WizardForm.Handle, SetupHwndTopMost"
    )
    assert activate.index("WizardForm.Handle, SetupHwndTopMost") < activate.index(
        "SetForegroundWindow(WizardForm.Handle)"
    )
    assert "SetupSwpNoActivate" in activate
    assert "InitialWizardFallbackTopMost := True;" in activate
    assert "ScheduleInitialWizardFallbackCleanup;" in activate

    remove_topmost = installer[
        installer.index("procedure RemoveInitialWizardTopMost") : installer.index(
            "procedure InitialWizardFallbackCleanupTimerProcedure"
        )
    ]
    assert remove_topmost.index("CancelInitialWizardFallbackCleanupTimer;") < remove_topmost.index(
        "if not InitialWizardFallbackTopMost then"
    )
    assert remove_topmost.index("InitialWizardFallbackTopMost := False;") < remove_topmost.index(
        "WizardForm.Handle, SetupHwndNotTopMost"
    )
    assert "SetupSwpNoActivate" in remove_topmost

    cleanup_timer = installer[
        installer.index("procedure InitialWizardFallbackCleanupTimerProcedure") : installer.index(
            "procedure InitialWizardActivated"
        )
    ]
    assert "CreateCallback(@InitialWizardFallbackCleanupTimerProcedure)" in cleanup_timer
    assert "InitialWizardFallbackCleanupMilliseconds" in cleanup_timer
    assert cleanup_timer.index("CancelInitialWizardFallbackCleanupTimer;") < cleanup_timer.index(
        "RemoveInitialWizardTopMost;"
    )

    activated = installer[
        installer.index("procedure InitialWizardActivated") : installer.index("procedure ActivateInitialWizard")
    ]
    assert "RemoveInitialWizardTopMost;" in activated

    initialize = installer[
        installer.index("procedure InitializeWizard") : installer.index("procedure DeinitializeSetup")
    ]
    assert "if not WizardSilent then" in initialize
    assert initialize.index("if not WizardSilent then") < initialize.index(
        "WizardForm.OnActivate := @InitialWizardActivated;"
    )
    assert initialize.index("WizardForm.OnActivate := @InitialWizardActivated;") < initialize.index(
        "WizardForm.OnShow := @ScheduleInitialWizardActivation;"
    )
    assert "WizardForm.OnShow := @ScheduleInitialWizardActivation;" in initialize

    deinitialize = installer[
        installer.index("procedure DeinitializeSetup") : installer.index("function InitializeUninstall")
    ]
    assert deinitialize.index("InitialWizardActivationShuttingDown := True;") < deinitialize.index(
        "CancelInitialWizardActivationTimer;"
    )
    assert deinitialize.index("CancelInitialWizardActivationTimer;") < deinitialize.index("RemoveInitialWizardTopMost;")
    assert deinitialize.index("RemoveInitialWizardTopMost;") < deinitialize.index("try")
    assert installer.count("BringToFrontAndRestore;") == 1
    assert installer.count("WizardForm.Handle, SetupHwndTopMost") == 1
    assert installer.count("WizardForm.Handle, SetupHwndNotTopMost") == 1
    for forbidden_focus_hack in (
        "AllowSetForegroundWindow",
        "AttachThreadInput",
        "keybd_event",
        "mouse_event",
        "SendInput",
    ):
        assert forbidden_focus_hack not in installer


def test_service_installer_stages_every_original_user_call_in_a_unique_programdata_leaf() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    prepare_transfer = installer[
        installer.index("function PrepareOriginalUserTransfer") : installer.index(
            "function ClearDesktopMigrationTransfer"
        )
    ]
    assert "TransferLeaf := ExtractFileName(ExpandConstant('{tmp}'));" in prepare_transfer
    assert "'{commonappdata}\\E-Rechnungs-Pruefer-Installer-Transfer') + '\\' + TransferLeaf" in prepare_transfer
    assert "OriginalUserOpenClientPath :=" in prepare_transfer
    assert "MigrationTransferDirectory + '\\{#OpenClientExeName}'" in prepare_transfer
    assert "MigrationTransferDirectory + '\\desktop-migration-receipt.json'" in prepare_transfer
    assert "MigrationTransferDirectory + '\\desktop-api-token-transfer.txt'" in prepare_transfer
    assert "--prepare-desktop-migration-transfer" in prepare_transfer
    assert '--client-source "' + "' + InternalOpenClient +" in prepare_transfer
    assert '--client-name "{#OpenClientExeName}"' in prepare_transfer
    assert "InternalOpenClient" in prepare_transfer

    original_exec = installer[
        installer.index("function ExecOriginalWithExitCode") : installer.index(
            "function CaptureOriginalServiceMetadata"
        )
    ]
    assert original_exec.count("OriginalUserOpenClientPath") == 2
    assert "{tmp}" not in original_exec
    assert "{app}" not in original_exec

    preflight = installer[
        installer.index("function PrepareToInstall") : installer.index("procedure ConfigureInstalledService")
    ]
    assert (
        preflight.index("AcquireSetupUninstallMutex")
        < preflight.index("ExtractTemporaryFile")
        < preflight.index("PrepareOriginalUserTransfer")
        < preflight.index("--assert-no-pending-service-uninstall")
        < preflight.index("ReconcilePendingInstall")
        < preflight.index("OriginalUserOpenClientPath, '--verify-migration-context'")
    )
    assert "InternalOpenClient, '--verify-migration-context'" not in preflight

    prepare_migration = installer[
        installer.index("function PrepareDesktopMigration") : installer.index("function BeginServiceTransition")
    ]
    assert r"{tmp}\desktop-migration-receipt.json" not in prepare_migration
    assert r"{tmp}\desktop-api-token-transfer.txt" not in prepare_migration
    assert '--transfer-directory "' + "' + MigrationTransferDirectory +" in prepare_migration
    assert '--client-name "{#OpenClientExeName}"' in prepare_migration

    clear_transfer = installer[
        installer.index("function ClearDesktopMigrationTransfer") : installer.index("function ExecOriginalWithExitCode")
    ]
    assert "--clear-desktop-migration-transfer" in clear_transfer
    assert "MigrationTransferDirectory = ''" in clear_transfer
    assert "DelTree(" not in clear_transfer
    assert "RemoveDir(" not in clear_transfer

    deinitialize = installer[
        installer.index("procedure DeinitializeSetup") : installer.index("function InitializeUninstall")
    ]
    rollback_body, cleanup_body = deinitialize.split("finally", maxsplit=1)
    assert rollback_body.index("if InstallSucceeded then") < rollback_body.index("if not SetupUninstallMutexOwned then")
    assert "ClearDesktopMigrationTransfer" not in rollback_body
    assert cleanup_body.index("if MigrationTransferDirectory <> '' then") < cleanup_body.index(
        "ClearDesktopMigrationTransfer"
    )
    assert cleanup_body.index("ClearDesktopMigrationTransfer") < cleanup_body.index("ReleaseSetupUninstallMutex")

    assert installer.count("ExecAsOriginalUser(") == 2
    assert installer.count("OriginalUserOpenClientPath") >= 5


def test_test_installer_logs_only_allowlisted_internal_open_client_diagnostics() -> None:
    installer = _read("packaging/windows/service_installer.iss")
    diagnostic_support = installer[
        installer.index("function IsKnownSetupDiagnosticStage") : installer.index("function ExecChecked")
    ]

    assert "#ifdef AllowElevatedMigrationTestContext" in diagnostic_support
    assert "setup-action-diagnostic-v1.txt" in installer
    assert "ERP_SETUP_DIAGNOSTIC_V1" in installer
    assert '--setup-diagnostic "' + "' + DiagnosticPath + '" in diagnostic_support
    assert "MigrationTransferDirectory + '\\' + SetupDiagnosticFileName" in diagnostic_support
    assert "CompareText(FileName, InternalOpenClient)" in diagnostic_support
    assert "CompareText(FileName, OriginalUserOpenClientPath)" not in diagnostic_support
    assert "FileSize(DiagnosticPath, DiagnosticSize)" in diagnostic_support
    assert "DiagnosticSize > 256" in diagnostic_support
    assert "LoadStringFromFile(DiagnosticPath, RawDiagnostic)" in diagnostic_support
    assert "ParseSetupDiagnostic(Diagnostic, Stage, ErrorCode, WinError)" in diagnostic_support
    assert "IsKnownSetupDiagnosticStage(Stage)" in diagnostic_support
    assert "IsKnownSetupDiagnosticError(ErrorCode)" in diagnostic_support
    assert "IsSetupDiagnosticWinError(WinError)" in diagnostic_support
    assert "DeleteFile(DiagnosticPath)" in diagnostic_support
    assert "GetExceptionMessage" not in diagnostic_support
    diagnostic_consumer = diagnostic_support[diagnostic_support.index("procedure ConsumeSetupDiagnostic") :]
    assert "+ DiagnosticPath" not in diagnostic_consumer
    assert "+ Diagnostic" not in diagnostic_consumer

    for safe_code in (
        "preflight-machine",
        "preflight-port",
        "plan-migration",
        "apply-migration",
        "runtime-error",
        "windows-api-error",
        "os-error",
        "internal-error",
    ):
        assert safe_code in diagnostic_support

    exec_checked = installer[installer.index("function ExecChecked") : installer.index("function ExecWithExitCode")]
    exec_with_exit_code = installer[
        installer.index("function ExecWithExitCode") : installer.index("function PrepareOriginalUserTransfer")
    ]
    for helper in (exec_checked, exec_with_exit_code):
        assert "AddSetupDiagnosticParameter" in helper
        assert "ConsumeSetupDiagnostic" in helper
        assert "ExitCode = 1" in helper
    original_exec = installer[
        installer.index("function ExecOriginalWithExitCode") : installer.index(
            "function CaptureOriginalServiceMetadata"
        )
    ]
    assert "AddSetupDiagnosticParameter" not in original_exec
    assert "ConsumeSetupDiagnostic" not in original_exec


def test_service_installer_orders_durable_migration_recovery_around_the_commit_point() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    for expected in (
        "--verify-desktop-migration-owner",
        "--prepare-install-reconcile",
        "--finish-install-reconcile",
        "--plan-desktop-migration",
        "--seal-desktop-migration",
        "--apply-desktop-migration",
        "--verify-applied-desktop-migration",
        "--begin-service-transition",
        "--mark-service-rollback-complete",
        "--mark-service-committed",
    ):
        assert expected in installer

    preflight = installer[
        installer.index("function PrepareToInstall") : installer.index("procedure ConfigureInstalledService")
    ]
    assert (
        preflight.index("ExtractTemporaryFile")
        < preflight.index("ReconcilePendingInstall")
        < preflight.index("--preflight-machine")
        < preflight.index("(not ServiceWasRunning) and CheckForMutexes(BackendMutexName)")
        < preflight.index("Loopback-Port bei gestopptem oder fehlendem Dienst vorprüfen")
        < preflight.index("PrepareDesktopMigration")
        < preflight.index("ForceDirectories(ExpandConstant('{app}'))")
        < preflight.index("BeginServiceTransition")
        < preflight.index("StopExistingServiceForUpdate")
        < preflight.rindex("CheckForMutexes(BackendMutexName)")
        < preflight.rindex("--preflight-port")
        < preflight.index("PrepareServiceBundleTransaction")
    )

    desktop_flow = installer[
        installer.index("function PrepareDesktopMigration") : installer.index("function BeginServiceTransition")
    ]
    assert (
        desktop_flow.index("--plan-desktop-migration")
        < desktop_flow.index("--seal-desktop-migration")
        < desktop_flow.index("if FileExists(TokenTransferFile) and not DeleteFile(TokenTransferFile)")
        < desktop_flow.index("if FileExists(MigrationReceipt) and not DeleteFile(MigrationReceipt)")
        < desktop_flow.index("--apply-desktop-migration")
        < desktop_flow.index("--verify-applied-desktop-migration")
    )
    assert "FileExists(TokenTransferFile) or FileExists(MigrationReceipt)" in desktop_flow

    begin_transition = installer[
        installer.index("function BeginServiceTransition") : installer.index("function MarkServiceCommitted")
    ]
    assert "--target-service-running " in begin_transition
    assert "--token-transfer-consent" in begin_transition
    assert "--snapshot-service-metadata" not in begin_transition

    pending_reconcile = installer[
        installer.index("{ A pending Desktop transaction") : installer.index("function PrepareDesktopMigration")
    ]
    assert (
        pending_reconcile.index("VerifyDesktopMigrationOwner")
        < pending_reconcile.index("FinishInstallReconcile(Direction)")
        < pending_reconcile.index("RollbackDesktopMigration")
        < pending_reconcile.index("ClearDesktopMigrationSeal")
        < pending_reconcile.index("FinalizeServiceBundle")
        < pending_reconcile.index("FinishTerminalInstallTransaction")
    )
    assert pending_reconcile.index("FinishInstallReconcile(Direction)") < pending_reconcile.index(
        "CommitDesktopMigration"
    )

    rollback_flow = installer[
        installer.index("function RollbackPreparedInstall") : installer.index("function InspectExistingService")
    ]
    assert (
        rollback_flow.index("RollbackServiceConfiguration")
        < rollback_flow.index("RollbackDesktopMigration")
        < rollback_flow.index("ClearDesktopMigrationSeal")
        < rollback_flow.index("FinishTerminalInstallTransaction")
    )

    done_step = installer[installer.index("procedure CurStepChanged") : installer.index("procedure InitializeWizard")]
    assert (
        done_step.index("CommitServiceBundle")
        < done_step.index("MarkServiceCommitted")
        < done_step.index("TransactionCommitStarted := True")
        < done_step.index("CommitDesktopMigration")
        < done_step.index("ClearDesktopMigration")
        < done_step.index("FinalizeServiceBundle")
        < done_step.index("FinishTerminalInstallTransaction")
        < done_step.index("InstallSucceeded := True")
    )
    assert done_step.index("TransactionCommitStarted := True") < done_step.index("CommitDesktopMigration")

    deinitialize = installer[
        installer.index("procedure DeinitializeSetup") : installer.index("function InitializeUninstall")
    ]
    assert "TransactionCommitStarted" in deinitialize
    assert deinitialize.index("TransactionCommitStarted") < deinitialize.index("RollbackServiceConfiguration")
    commit_branch = deinitialize[
        deinitialize.index("if TransactionCommitStarted then") : deinitialize.index(
            "if ServiceTransactionPrepared and not RollbackServiceConfiguration"
        )
    ]
    assert "RollbackServiceConfiguration" not in commit_branch
    assert (
        commit_branch.index("CommitDesktopMigration")
        < commit_branch.index("ClearDesktopMigrationSeal")
        < commit_branch.index("FinalizeServiceBundle")
        < commit_branch.index("FinishTerminalInstallTransaction")
    )

    configure = installer[
        installer.index("procedure ConfigureInstalledService") : installer.index("procedure CurStepChanged")
    ]
    assert "ProtectedDesktopTokenFile" in configure
    assert "TokenTransferFile" not in configure
    assert r"{commonappdata}\E-Rechnungs-Pruefer-Installer-State\desktop-api-token.txt" in installer

    service_preparation = installer[
        installer.index("function PrepareServiceBundleTransaction") : installer.index(
            "procedure ActivateStagedServiceBundle"
        )
    ]
    assert "DeleteTreeIfPresent" not in service_preparation
    assert "RenameFile" not in service_preparation


def test_windows_build_signs_owned_binaries_and_both_installers() -> None:
    script = _read("scripts/build_windows.ps1")

    for expected in (
        "e_rechnungs_pruefer.spec",
        "e_rechnungs_pruefer_service.spec",
        "e_rechnungs_pruefer_open_client.spec",
        'Sign-File (Join-Path $DesktopBundle "E-Rechnungs-Pruefer.exe")',
        'Sign-File (Join-Path $ServiceBundle "E-Rechnungs-Pruefer-Dienst.exe")',
        "Sign-File $OpenClient",
        "Sign-File $DesktopSetup",
        "Sign-File $ServiceSetup",
        "Windows-x64-SHA256SUMS.txt",
        "Windows-x64-Binaries.zip",
        "Compress-Archive",
        "$OwnedFiles",
        "BuildElevatedMigrationTestInstaller",
        "/DAllowElevatedMigrationTestContext=1",
        "$TestInstallerRoot",
        "Sign-File $ElevatedMigrationTestSetup",
        "function Test-PublishedWindowsArtifacts",
        "Expand-Archive -LiteralPath $Archive -DestinationPath $VerificationRoot",
        "publish-verification-$([guid]::NewGuid().ToString('N'))",
        r"$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        r"$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
    ):
        assert expected in script

    assert "Get-ChildItem $ServiceBundle -Recurse" not in script
    owned_files = script[script.index("$OwnedFiles = @(") : script.index("$ChecksumLines =")]
    assert "ElevatedMigrationTestSetup" not in owned_files
    verification = script[
        script.index("function Test-PublishedWindowsArtifacts") : script.index("if ($SigningEnabled)")
    ]
    for expected in (
        "$ExpectedPaths.Count -ne 6",
        "$ManifestLines.Count -ne 6",
        "[System.Collections.Generic.HashSet[string]]",
        "Copy-Item -LiteralPath $DesktopInstaller -Destination $VerificationRoot",
        "Copy-Item -LiteralPath $ServiceInstaller -Destination $VerificationRoot",
        "Copy-Item -LiteralPath $Archive -Destination $VerificationRoot",
        "[System.IO.File]::ReadAllLines($Manifest)",
        "[0-9A-Fa-f]{64})  (?<RelativePath>[^\\s]+)",
        "[System.IO.Path]::IsPathRooted($RelativePath)",
        "$RelativePath.Contains('\\')",
        "@('', '.', '..')",
        "$ExpectedPathSet.Contains($RelativePath)",
        "$VerifiedPathSet.Add($RelativePath)",
        "[System.IO.Path]::GetFullPath(",
        "$ArtifactPath.StartsWith(",
        "Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256",
        "$VerifiedPathSet.Contains($ExpectedPath)",
        "Remove-Item -LiteralPath $VerificationRoot -Recurse -Force",
    ):
        assert expected in verification
    assert script.index("Set-Content $ChecksumFile") < script.rindex("Test-PublishedWindowsArtifacts")
    expected_paths = script[script.index("$ExpectedPublishedPaths = @(") : script.index("$VerificationRoot =")]
    assert expected_paths.count('"bundle/') == 3
    assert expected_paths.count('"E-Rechnungs-Pruefer-$Version-Windows-x64-') == 3
    service_spec = _read("packaging/windows/e_rechnungs_pruefer_service.spec")
    open_client_spec = _read("packaging/windows/e_rechnungs_pruefer_open_client.spec")
    assert "disable_windowed_traceback=True" in service_spec
    assert '"win32net"' in service_spec
    assert '"win32net"' in open_client_spec
    service_entrypoint = _read("packaging/windows/service_entrypoint.py")
    assert "raise SystemExit(_run(sys.argv[1:]))" in service_entrypoint
    assert "if session_id is None or session_id == 0:" in service_entrypoint
    assert "E-Rechnungs-Pruefer-Oeffnen.exe" in service_entrypoint


def test_windows_ci_builds_and_tests_both_modes() -> None:
    ci = _read(".github/workflows/ci.yml")
    release = _read(".github/workflows/release.yml")

    for workflow in (ci, release):
        assert r".\scripts\test_windows_package.ps1" in workflow
        assert r".\scripts\test_windows_service_package.ps1" in workflow
        assert "-BuildElevatedMigrationTestInstaller" in workflow
        assert workflow.count("-AllowElevatedMigrationTestContext") == 2
        assert "*-Windows-x64-Dienst-Setup.exe" in workflow
        assert "*-Windows-x64-Binaries.zip" in workflow
        assert "*-Windows-x64-SHA256SUMS.txt" in workflow

    for executable in (
        "E-Rechnungs-Pruefer.exe",
        "E-Rechnungs-Pruefer-Dienst.exe",
        "E-Rechnungs-Pruefer-Oeffnen.exe",
    ):
        assert executable in release


def test_service_package_test_covers_scm_acl_update_and_uninstall_contract() -> None:
    script = _read("scripts/test_windows_service_package.ps1")

    for expected in (
        "-ConfirmIsolatedEnvironment",
        "ERechnungsPrueferService",
        "NT AUTHORITY\\LocalService",
        "DelayedAutoStart",
        "Get-Acl",
        "S-1-1-0",
        "S-1-5-11",
        "S-1-5-32-545",
        "Authorization: Bearer",
        "/api/report/pdf",
        "/api/xml",
        "official=false",
        "Get-FileHash",
        "Get-AuthenticodeSignature",
        "Invoke-ServiceInstaller",
        "ExpectedLogReason",
        "Assert-NoEarlyInstallerState",
        "E-Rechnungs-Pruefer-Installer-State",
        "E-Rechnungs-Pruefer-Installer-Transfer",
        ".installer-state",
        "Assert-TokenReaderAcl",
        "Assert-ProtectedLogAcl",
        "Add-ExplorerAdministratorDirectoryAce",
        "Invoke-WindowedExecutable",
        "Get-LocalGroupMember -SID",
        "ConfigurationHashBeforePreserve",
        "LogBytesBeforePreserve",
        "LogPrefixPreserved",
        "S-1-3-4",
        "ReadPermissions",
        "--grant-token-read",
        "--rotate-token",
        "$ReaderSid",
        "TokenBeforeRestart",
        "TokenBeforeUpdate",
        "/PURGEDATA=1",
        "/TESTFAILAFTERCONFIG=1",
        "Rollback-Test-Beschreibung",
        "FailureActionsBeforeFailedUpdate",
        "FailureActionsOnNonCrashFailures",
        "qdescription",
        "qfailureflag",
        "qsidtype",
        "FailureActionsScmBeforeFailedUpdate",
        "Get-TreeFingerprint",
        "rollback-sentinel.txt",
        "service.new",
        "service.rollback",
        "service.obsolete",
        'Invoke-ServiceInstaller -Path $Setup -LogPath $StoppedUpdateLog -Tasks ""',
        "PortBlocker",
        "programdata-junction-target",
        "ItemType Junction",
        "Wait-ServiceProcessRestart",
        "GetTempPath",
        "Get-Service -Name $ServiceName",
        "AllowElevatedMigrationTestContext",
        r"build\windows\test-installer",
        "CommitHardKillRecovery",
        "Invoke-CommitCheckpointHardKill",
        "install-transaction.phase.json",
        "commit_started",
        "Stop-VerifiedSetupProcessTree",
    ):
        assert expected in script

    assert script.count('"/ALLOWELEVATEDTESTCONTEXT=1"') == 3
    assert script.count("if ($AllowElevatedMigrationTestContext)") == 4
    assert script.count("-ExpectedLogReason") == 4
    assert script.count("Assert-NoEarlyInstallerState -Scenario") == 3
    for expected_reason in (
        "Die ursprüngliche interaktive Benutzeridentität konnte nicht sicher bestätigt werden.",
        "Der vorhandene Maschinenzustand ist unvollständig, unsicher oder ungültig.",
        "Der konfigurierte lokale Dienstport ist belegt oder nicht exklusiv reservierbar.",
        "Absichtlich ausgelöster transaktionaler Installationstest.",
    ):
        assert expected_reason in script
    assert script.count("Add-ExplorerAdministratorDirectoryAce -Path") == 4
    assert script.count("Invoke-WindowedExecutable -Path $ServiceExe") == 3
    first_repair = script.index("Add-ExplorerAdministratorDirectoryAce -Path $DataDir")
    assert first_repair < script.index('Arguments @("--verify-state")', first_repair)
    preserve_repair = script.rindex("Add-ExplorerAdministratorDirectoryAce -Path $DataDir")
    assert preserve_repair < script.index("Invoke-ServiceUninstaller", preserve_repair)


def test_migration_test_uses_published_130_desktop_installer() -> None:
    script = _read("scripts/test_windows_migration.ps1")

    for expected in (
        "v1.3.0",
        "E-Rechnungs-Pruefer-1.3.0-Windows-x64-Setup.exe",
        "/MIGRATEDESKTOPTOKEN=1",
        "EINVOICE_API_TOKEN",
        "E-Rechnungs-Pruefer",
        "ERechnungsPrueferService",
        "ConfirmIsolatedEnvironment",
        "TESTFAILAFTERCONFIG",
        "Assert-MigratedTokenAcl",
        "service-mode-disabled",
        "E-Rechnungs-Pruefer-Installer-State",
        "ALLOWELEVATEDTESTCONTEXT",
        "AllowElevatedMigrationTestContext",
        r"build\windows\test-installer",
        "GetTempPath",
        "DesktopHardKillRecovery",
        "Invoke-DesktopCheckpointHardKill",
        "desktop-migration-phase.json",
        "token_scrypt",
        "hashlib.scrypt",
        "rollbackable",
        "Stop-VerifiedSetupProcessTree",
    ):
        assert expected in script

    assert script.count('"/ALLOWELEVATEDTESTCONTEXT=1"') == 3
    assert script.count("if ($AllowElevatedMigrationTestContext)") == 3
