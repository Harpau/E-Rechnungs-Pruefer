from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_desktop_installer_remains_a_separate_unprivileged_option() -> None:
    installer = _read("packaging/windows/installer.iss")

    assert "AppId={{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}" in installer
    assert r"DefaultDirName={localappdata}\Programs\E-Rechnungs-Pruefer" in installer
    assert "DisableDirPage=yes" not in installer
    assert "PrivilegesRequired=lowest" in installer
    assert 'Root: HKCU; Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"' in installer
    assert 'Name: "autostart"' in installer
    assert "function ServiceFootprintExists: Boolean;" in installer
    assert "'SYSTEM\\CurrentControlSet\\Services\\ERechnungsPrueferService'" in installer
    assert (
        "'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{8824D15C-7F4E-4CB2-B957-FBC26B923363}_is1'"
    ) in installer
    assert "DirExists(ExpandConstant('{autopf64}\\E-Rechnungs-Pruefer-Dienst'))" in installer
    assert "{commonappdata}\\E-Rechnungs-Pruefer" not in installer
    assert "if ServiceFootprintExists then" in installer
    assert 'unter "Installierte Apps"' in installer

    prepare = installer[installer.index("function PrepareToInstall") : installer.index("procedure DeinitializeSetup")]
    assert prepare.index("AcquireSetupUninstallMutex") < prepare.index("ServiceFootprintExists")
    assert prepare.index("ServiceFootprintExists") < prepare.index("StopRunningApplication")
    assert (
        "ReleaseSetupUninstallMutex;"
        in installer[
            installer.index("procedure DeinitializeSetup") : installer.index(
                "function ShouldRestartBackgroundAfterUpdate"
            )
        ]
    )
    assert (
        "AcquireSetupUninstallMutex"
        in installer[
            installer.index("function InitializeUninstall") : installer.index("procedure DeinitializeUninstall")
        ]
    )
    assert "ReleaseSetupUninstallMutex;" in installer[installer.index("procedure DeinitializeUninstall") :]


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
        "--assert-no-desktop-installation",
        "--preflight-machine",
        "--preflight-port",
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
        "--verify-state",
        "PurgeMachineData",
        "MB_DEFBUTTON2",
        "PurgeOwnedMachineState",
        "PurgeTransientRuntimeState",
        "--purge-runtime-state",
        "--purge-machine-state",
        "RemoveOwnedServiceDirectories",
        "#ifdef AllowElevatedRecoveryTestContext",
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
    for removed in (
        "TokenMigrationPage",
        "MIGRATEDESKTOPTOKEN",
        "ExecAsOriginalUser",
        "PrepareOriginalUserTransfer",
        "PrepareDesktopMigration",
        "CommitDesktopMigration",
        "RollbackDesktopMigration",
        "DesktopHardKill",
        "--import-token",
        "--consent-token-import",
        "--token-transfer-consent",
        "--verify-migration-context",
        "--commit-desktop-migration",
        "--clear-desktop-migration-seal",
        "--setup-diagnostic",
    ):
        assert removed not in installer
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
    assert (
        done_step.index("CommitServiceBundle")
        < done_step.index("MarkServiceCommitted")
        < done_step.index("FinalizeServiceBundle")
        < done_step.index("FinishTerminalInstallTransaction")
        < done_step.index("InstallSucceeded := True;")
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
    assert (
        preflight.index("--assert-no-desktop-installation")
        < preflight.index("--assert-no-pending-service-uninstall")
        < preflight.index("ReconcilePendingInstall")
    )
    assert preflight.count("--assert-no-desktop-installation") == 2
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
        < preflight.rindex("--assert-no-desktop-installation")
        < preflight.index("ForceDirectories(ExpandConstant('{app}'))")
        < preflight.index("BeginServiceTransition")
        < preflight.index("StopExistingServiceForUpdate")
        < preflight.rindex("CheckForMutexes(BackendMutexName)")
        < preflight.rindex("--preflight-port")
        < preflight.index("PrepareServiceBundleTransaction")
    )
    initial_port_conflict = preflight[
        preflight.index("Loopback-Port bei gestopptem oder fehlendem Dienst vorprüfen") : preflight.index(
            "ForceDirectories(ExpandConstant('{app}'))"
        )
    ]
    assert "if not RollbackPreparedInstall then" not in initial_port_conflict
    assert "vor dem Ersetzen von Dienstbinärdateien" in initial_port_conflict


def test_service_configuration_failures_abort_through_the_transactional_install_path() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    files = installer[installer.index("[Files]") : installer.index("[Icons]")]
    configured_bundle_entry = (
        'Source: "{#ProjectRoot}\\THIRD_PARTY.md"; DestDir: "{app}\\service.new"; '
        "Flags: ignoreversion uninsneveruninstall; AfterInstall: ConfigureInstalledService"
    )
    failure_sentinel_entry = (
        'Source: "{#ProjectRoot}\\LICENSE"; '
        'DestDir: "{code:PropagateServiceConfigurationFailure}"; '
        "Flags: ignoreversion; Check: ServiceConfigurationHasFailed"
    )
    assert configured_bundle_entry in files
    assert failure_sentinel_entry in files
    assert files.index(configured_bundle_entry) < files.index(failure_sentinel_entry)

    record_failure = installer[
        installer.index("procedure RecordServiceConfigurationFailure") : installer.index(
            "function ServiceConfigurationHasFailed"
        )
    ]
    assert "ServiceConfigurationFailed := True;" in record_failure
    assert "ServiceConfigurationFailureReason := Reason;" in record_failure
    assert "Dienstkonfiguration wurde nicht vollständig abgeschlossen" in record_failure

    propagate_failure = installer[
        installer.index("function PropagateServiceConfigurationFailure") : installer.index(
            "#ifdef AllowElevatedRecoveryTestContext",
            installer.index("function PropagateServiceConfigurationFailure"),
        )
    ]
    assert "SuppressibleMsgBox(" in propagate_failure
    assert "ServiceConfigurationFailureReason" in propagate_failure
    assert "finally" in propagate_failure
    assert "Abort;" in propagate_failure
    assert "RaiseException(" not in propagate_failure

    test_fault_start = installer.index(
        "#ifdef AllowElevatedRecoveryTestContext",
        installer.index("procedure RecordServiceConfigurationFailure"),
    )
    test_fault = installer[test_fault_start : installer.index("#endif", test_fault_start)]
    assert "{param:ALLOWELEVATEDTESTCONTEXT|0}" in test_fault
    assert "{param:TESTFAILAFTERCONFIG|0}" in test_fault

    configure = installer[
        installer.index("procedure ConfigureInstalledService") : installer.index("procedure CurStepChanged")
    ]
    assert "try" in configure
    assert "except" in configure
    assert "RecordServiceConfigurationFailure(GetExceptionMessage);" in configure
    injected_failure = configure.index("Absichtlich ausgelöster transaktionaler Installationstest.")
    assert configure.rindex("RecordServiceConfigurationFailure(", 0, injected_failure) < injected_failure
    assert injected_failure < configure.index("Exit;", injected_failure)
    assert configure.index("Exit;", injected_failure) < configure.index("Sc('start")
    assert "RaiseException('Absichtlich ausgelöster transaktionaler Installationstest.')" not in installer

    custom_exit = installer[
        installer.index("function GetCustomSetupExitCode") : installer.index("procedure DeinitializeSetup")
    ]
    assert "Result := 0;" in custom_exit
    assert "if ServicePrepared and not InstallSucceeded then" in custom_exit
    assert "Result := 4;" in custom_exit


def test_service_installer_serializes_setup_and_uninstall_mutations() -> None:
    installer = _read("packaging/windows/service_installer.iss")
    package_test = _read("scripts/test_windows_service_package.ps1")

    assert "SetupUninstallMutexName = 'Global\\E-Rechnungs-Pruefer-Setup-Uninstall';" in installer
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
    assert deinitialize_uninstall.index("ClearOriginalServiceMetadata") < deinitialize_uninstall.index(
        "ReleaseSetupUninstallMutex;"
    )
    assert deinitialize_uninstall.rindex("ReleaseSetupUninstallMutex;") > deinitialize_uninstall.rindex("finally")
    assert "RemoveDir(" not in deinitialize_uninstall
    assert "DelTree(" not in deinitialize_uninstall

    wait_for_release = package_test[
        package_test.index("function Wait-SetupUninstallMutexReleased") : package_test.index(
            "function Wait-PathsAbsent"
        )
    ]
    assert '"Global\\E-Rechnungs-Pruefer-Setup-Uninstall"' in wait_for_release
    assert "[Threading.Mutex]::OpenExisting($Name)" in wait_for_release
    assert "$Mutex.WaitOne($Seconds * 1000)" in wait_for_release
    assert "catch [Threading.WaitHandleCannotBeOpenedException]" in wait_for_release
    assert "catch [Threading.AbandonedMutexException]" in wait_for_release
    assert "$Abandoned = $true" in wait_for_release
    assert "Die temporäre zweite Uninstaller-Phase wurde unerwartet beendet." in wait_for_release
    assert wait_for_release.index("$Mutex.ReleaseMutex()") < wait_for_release.index("$Mutex.Dispose()")

    invoke_uninstaller = package_test[
        package_test.index("function Invoke-ServiceUninstaller") : package_test.index(
            "function Wait-SetupUninstallMutexReleased"
        )
    ]
    assert (
        invoke_uninstaller.index("$Process.WaitForExit(600000)")
        < invoke_uninstaller.index("if ($Process.ExitCode -ne 0)")
        < invoke_uninstaller.index("Wait-SetupUninstallMutexReleased")
    )


def test_service_package_waits_for_inno_cleanup_before_reinstall_and_success() -> None:
    script = _read("scripts/test_windows_service_package.ps1")
    installer = _read("packaging/windows/service_installer.iss")

    uninstall_delete = installer[installer.index("[UninstallDelete]") : installer.index("[Code]")]
    assert installer.count("[UninstallDelete]") == 1
    uninstall_delete_entries = [
        line.strip()
        for line in uninstall_delete.splitlines()
        if line.strip() and not line.startswith("[UninstallDelete]")
    ]
    assert uninstall_delete_entries == ['Type: dirifempty; Name: "{app}"']
    assert "Type: files;" not in uninstall_delete
    assert "filesandordirs" not in uninstall_delete
    assert "{commonappdata}" not in uninstall_delete
    assert "*" not in uninstall_delete

    wait_for_paths = script[script.index("function Wait-PathsAbsent") : script.index("function Assert-ValidSignature")]
    for expected in (
        "[Parameter(Mandatory = $true)]",
        "[string[]]$LiteralPath",
        "[ValidateRange(1, 600)]",
        "[int]$Seconds = 60",
        "[Diagnostics.Stopwatch]::StartNew()",
        "Test-Path -LiteralPath $Candidate",
        "$Timer.Elapsed.TotalSeconds -ge $Seconds",
        "Start-Sleep -Milliseconds 250",
        "Noch vorhanden: $($Remaining -join ', ')",
    ):
        assert expected in wait_for_paths
    assert "Remove-Item" not in wait_for_paths
    assert "Remove-ItemProperty" not in wait_for_paths

    preserve_start = script.index("Invoke-ServiceUninstaller -Path $Uninstaller -LogPath $UninstallLog")
    preserve_end = script.index(
        "if ((Get-Content $TokenFile -Raw).Trim() -ne $TokenBeforeUpdate)",
        preserve_start,
    )
    preserve = script[preserve_start:preserve_end]
    assert (
        preserve.index("Invoke-ServiceUninstaller")
        < preserve.index('Wait-ServiceState -Name $ServiceName -State "Absent"')
        < preserve.index("Wait-PathsAbsent -LiteralPath @($InstallDir, $UninstallKey)")
        < preserve.index("Invoke-ServiceInstaller -Path $Setup -LogPath $ReinstallLog")
    )
    preserve_waits = [line for line in preserve.splitlines() if line.startswith("Wait-PathsAbsent ")]
    assert preserve_waits == ["Wait-PathsAbsent -LiteralPath @($InstallDir, $UninstallKey)"]
    assert "$DataDir" not in preserve_waits[0]

    purge_start = script.index("Invoke-ServiceUninstaller -Path $Uninstaller -LogPath $PurgeLog -PurgeData")
    purge = script[purge_start : script.index('Write-Host "Windows-Dienstpaket erfolgreich geprüft."', purge_start)]
    assert (
        purge.index("Invoke-ServiceUninstaller")
        < purge.index('Wait-ServiceState -Name $ServiceName -State "Absent"')
        < purge.index("Wait-PathsAbsent -LiteralPath @($InstallDir, $DataDir, $UninstallKey)")
    )


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


def test_service_installer_contains_no_desktop_migration_or_diagnostic_bridge() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    for removed in (
        "MigrationTransferDirectory",
        "OriginalUserOpenClientPath",
        "ExecAsOriginalUser",
        "DesktopMigration",
        "desktop-migration",
        "MIGRATEDESKTOPTOKEN",
        "TokenMigrationPage",
        "DesktopHardKill",
        "TESTDESKTOPHARDKILLHOLD",
        "SetupDiagnostic",
        "--setup-diagnostic",
        "Installer-Transfer",
    ):
        assert removed not in installer


def test_service_installer_orders_durable_service_only_recovery_around_commit() -> None:
    installer = _read("packaging/windows/service_installer.iss")

    preflight = installer[
        installer.index("function PrepareToInstall") : installer.index("procedure ConfigureInstalledService")
    ]
    assert (
        preflight.index("ExtractTemporaryFile")
        < preflight.index("--assert-no-desktop-installation")
        < preflight.index("ReconcilePendingInstall")
        < preflight.index("--preflight-machine")
        < preflight.index("(not ServiceWasRunning) and CheckForMutexes(BackendMutexName)")
        < preflight.index("Loopback-Port bei gestopptem oder fehlendem Dienst vorprüfen")
        < preflight.rindex("--assert-no-desktop-installation")
        < preflight.index("ForceDirectories(ExpandConstant('{app}'))")
        < preflight.index("BeginServiceTransition")
        < preflight.index("StopExistingServiceForUpdate")
        < preflight.rindex("CheckForMutexes(BackendMutexName)")
        < preflight.rindex("--preflight-port")
        < preflight.index("PrepareServiceBundleTransaction")
    )

    begin_transition = installer[
        installer.index("function BeginServiceTransition") : installer.index("function MarkServiceCommitted")
    ]
    assert "--target-service-running " in begin_transition
    assert "--token-transfer-consent" not in begin_transition

    pending_reconcile = installer[
        installer.index("function ReconcilePendingInstall") : installer.index("function BeginServiceTransition")
    ]
    assert (
        pending_reconcile.index("FinishInstallReconcile(Direction)")
        < pending_reconcile.index("FinalizeServiceBundle")
        < pending_reconcile.index("FinishTerminalInstallTransaction")
    )

    rollback_flow = installer[
        installer.index("function RollbackPreparedInstall") : installer.index("function InspectExistingService")
    ]
    assert rollback_flow.index("RollbackServiceConfiguration") < rollback_flow.index("FinishTerminalInstallTransaction")

    done_step = installer[installer.index("procedure CurStepChanged") : installer.index("procedure InitializeWizard")]
    assert (
        done_step.index("CommitServiceBundle")
        < done_step.index("MarkServiceCommitted")
        < done_step.index("TransactionCommitStarted := True")
        < done_step.index("FinalizeServiceBundle")
        < done_step.index("FinishTerminalInstallTransaction")
        < done_step.index("InstallSucceeded := True")
    )

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
    assert commit_branch.index("FinalizeServiceBundle") < commit_branch.index("FinishTerminalInstallTransaction")

    configure = installer[
        installer.index("procedure ConfigureInstalledService") : installer.index("procedure CurStepChanged")
    ]
    assert "ServiceExe, '--initialize'" in configure
    assert "--import-token" not in configure
    assert "--consent-token-import" not in configure

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
        "BuildElevatedRecoveryTestInstaller",
        "/DAllowElevatedRecoveryTestContext=1",
        "$TestInstallerRoot",
        "Sign-File $ElevatedRecoveryTestSetup",
        "function Test-PublishedWindowsArtifacts",
        "Expand-Archive -LiteralPath $Archive -DestinationPath $VerificationRoot",
        "publish-verification-$([guid]::NewGuid().ToString('N'))",
        r"$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        r"$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
    ):
        assert expected in script

    assert "Get-ChildItem $ServiceBundle -Recurse" not in script
    owned_files = script[script.index("$OwnedFiles = @(") : script.index("$ChecksumLines =")]
    assert "ElevatedRecoveryTestSetup" not in owned_files
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
    assert 'copy_metadata("regipy")' in open_client_spec
    assert '"regipy"' in open_client_spec
    assert '"regipy.registry"' in open_client_spec
    service_entrypoint = _read("packaging/windows/service_entrypoint.py")
    assert "raise SystemExit(_run(sys.argv[1:]))" in service_entrypoint
    assert "if session_id is None or session_id == 0:" in service_entrypoint
    assert "E-Rechnungs-Pruefer-Oeffnen.exe" in service_entrypoint


def test_offline_profile_inventory_is_read_only_bounded_and_version_pinned() -> None:
    scanner = _read("app/windows_install_conflicts.py")
    build_requirements = _read("packaging/windows/requirements-build.txt")
    release_requirements = _read("packaging/windows/requirements-release.txt")
    third_party = _read("THIRD_PARTY.md")

    for forbidden in ("RegLoadAppKeyW", "RegLoadKey", "RegRestoreKey", "reg.exe load"):
        assert forbidden not in scanner
    for expected in (
        'REGIPY_VERSION = "6.2.1"',
        "OFFLINE_HIVE_MAX_BYTES = 256 * 1024 * 1024",
        "FILE_FLAG_OPEN_REPARSE_POINT",
        "FILE_SHARE_READ",
        "_read_safe_hive_bytes",
        "_validate_hive_snapshot",
        "primary_sequence != secondary_sequence",
        "_registry_header_checksum",
        "_root_key_cell",
        "root_key_offset % 8 != 0",
        "OFFLINE_HIVE_INSPECTION_TIMEOUT_SECONDS = 30",
        "OFFLINE_PROFILE_INVENTORY_TIMEOUT_SECONDS = 60",
        "_inspect_offline_profile_hive_isolated",
        "subprocess.TimeoutExpired",
        "_validated_hive_bin_ranges",
        '!= b"hbin"',
        "len(children) != int(current.subkey_count)",
        "len(values) != int(current.values_count)",
    ):
        assert expected in scanner
    assert "regipy==6.2.1" in build_requirements
    assert (
        "regipy==6.2.1 --hash=sha256:b03110e5c4e12385e1ba53c032ccd120c6dcde1b71afb8c3b7aa4717a5a24e43"
    ) in release_requirements
    for expected in ("| Regipy |", "| Construct |", "| Inflection |", "| pytz |"):
        assert expected in third_party


def test_windows_ci_builds_and_tests_both_modes() -> None:
    ci = _read(".github/workflows/ci.yml")
    release = _read(".github/workflows/release.yml")

    for workflow in (ci, release):
        assert r".\scripts\test_windows_package.ps1" in workflow
        assert r".\scripts\test_windows_mode_exclusion.ps1" in workflow
        assert r".\scripts\test_windows_service_package.ps1" in workflow
        assert "-BuildElevatedRecoveryTestInstaller" in workflow
        assert workflow.count("-AllowElevatedRecoveryTestContext") == 1
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
        "Get-AccountSidType",
        "SIDType",
        "GetOwnerSid",
        "S-1-5-19",
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
        "Read-FileBytesWithRetry",
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
        "AllowElevatedRecoveryTestContext",
        r"build\windows\test-installer",
        "CommitHardKillRecovery",
        "Invoke-CommitCheckpointHardKill",
        "install-transaction.phase.json",
        "commit_started",
        "Stop-VerifiedSetupProcessTree",
    ):
        assert expected in script

    assert 'Get-ItemPropertyValue $ServiceRegistryPath "FailureActions"' not in script
    assert script.count("Read-FileBytesWithRetry -Path") == 2
    assert "[IO.File]::ReadAllBytes($LogFile)" not in script
    assert "[IO.File]::ReadAllBytes($Candidate)" not in script
    shared_log_read = script[
        script.index("function Read-FileBytesWithRetry") : script.index("function Invoke-ServiceInstaller")
    ]
    assert "[IO.FileStream]::new(" in shared_log_read
    assert "[IO.FileShare]::ReadWrite" in shared_log_read
    assert "[IO.FileShare]::Delete" in shared_log_read
    assert "[IO.FileAccess]::Read" in shared_log_read
    assert "finally" in shared_log_read
    assert "$Stream.Dispose()" in shared_log_read
    assert "$FailureActionsScmAfterFailedUpdate -ne $FailureActionsScmBeforeFailedUpdate" in script
    assert "Vorher:`n$FailureActionsScmBeforeFailedUpdate" in script
    assert "Nachher:`n$FailureActionsScmAfterFailedUpdate" in script
    assert script.count('"/ALLOWELEVATEDTESTCONTEXT=1"') >= 2
    assert script.count("if ($AllowElevatedRecoveryTestContext)") >= 2
    assert script.count("-ExpectedLogReason") == 3
    assert script.count("Assert-NoEarlyInstallerState -Scenario") == 2
    for expected_reason in (
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


def test_service_package_test_uses_locale_neutral_account_identifiers() -> None:
    script = _read("scripts/test_windows_service_package.ps1")

    account_type_helper = script[
        script.index("function Get-AccountSidType") : script.index("function Wait-ServiceState")
    ]
    assert "Get-CimInstance -ClassName Win32_Account" in account_type_helper
    assert "SIDType" in account_type_helper
    assert "$Accounts.Count -ne 1" in account_type_helper
    assert "$SidType -notin 1..9" in account_type_helper

    direct_administrator_check = script[
        script.index("$AdministratorsSid =") : script.index(
            "if ($CommitHardKillRecovery",
        )
    ]
    assert "Get-LocalGroupMember -SID $AdministratorsSid" in direct_administrator_check
    assert "Get-AccountSidType" in direct_administrator_check
    assert ".ObjectClass" not in direct_administrator_check
    assert '"User"' not in direct_administrator_check
    assert '"Benutzer"' not in direct_administrator_check

    service_account_check = script[
        script.index("$CimService = Get-CimInstance Win32_Service") : script.index(
            "$DelayedAutoStart =",
        )
    ]
    assert "Get-CimInstance Win32_Process" in service_account_check
    assert "Invoke-CimMethod" in service_account_check
    assert "GetOwnerSid" in service_account_check
    assert "S-1-5-19" in service_account_check
    assert "$CimService.StartName -ne" not in service_account_check
    assert "NT AUTHORITY\\LocalService" not in service_account_check
    assert "NT-AUTORITÄT\\Lokaler Dienst" not in service_account_check


def test_mode_exclusion_test_proves_manual_switch_and_no_mutation_on_rejection() -> None:
    script = _read("scripts/test_windows_mode_exclusion.ps1")

    for expected in (
        "ConfirmIsolatedEnvironment",
        "E-Rechnungs-Pruefer.exe",
        "ERechnungsPrueferService",
        "Invoke-SetupExpectedFailure",
        "Dienstinstallation bei vorhandenem Desktopmodus",
        "Desktopinstallation bei vorhandenem Dienstmodus",
        "Dienstinstallation nach Desktopdeinstallation",
        "Assert-RegistryValueUnchanged",
        "Assert-ServiceSnapshotUnchanged",
        "Get-TreeFingerprint",
        "DesktopTokenHashBefore",
        "ServiceTokenHashBefore",
        "Get-FileHash",
        "Get-AuthenticodeSignature",
        "Desktopdeinstallation",
        "Dienstdeinstallation",
        "Dienstdeinstallation mit erhaltenem ProgramData",
        "Desktopinstallation bei reinem erhaltenem ProgramData",
        "Dienstneuinstallation mit erhaltenem ProgramData",
        "ProgramData oder das Diensttoken",
        "Get-SanitizedInnoLogTail",
        "Write-ProfileHiveCategoryDiagnostic",
        "Write-LoopbackPortCategoryDiagnostic",
    ):
        assert expected in script

    assert "MIGRATEDESKTOPTOKEN" not in script
    assert "AllowElevatedRecoveryTestContext" not in script
    assert r"build\windows\test-installer" not in script
    assert "DesktopHardKill" not in script
    assert "TESTDESKTOPHARDKILLHOLD" not in script
    wait_for_setup_release = script[
        script.index("function Wait-SetupUninstallMutexReleased") : script.index("function Invoke-Setup")
    ]
    assert '"Global\\E-Rechnungs-Pruefer-Setup-Uninstall"' in wait_for_setup_release
    assert "[Threading.Mutex]::OpenExisting($Name)" in wait_for_setup_release
    assert "$Mutex.WaitOne($Seconds * 1000)" in wait_for_setup_release
    assert "catch [Threading.AbandonedMutexException]" in wait_for_setup_release
    invoke_setup = script[script.index("function Invoke-Setup") : script.index("function Invoke-SetupExpectedFailure")]
    invoke_expected_failure = script[
        script.index("function Invoke-SetupExpectedFailure") : script.index("function Wait-ServiceState")
    ]
    assert invoke_setup.index("$Process.WaitForExit(600000)") < invoke_setup.index("Wait-SetupUninstallMutexReleased")
    assert invoke_expected_failure.index("$Process.WaitForExit(600000)") < invoke_expected_failure.index(
        "Wait-SetupUninstallMutexReleased"
    )
    assert "$ExitCode -eq 0" in invoke_expected_failure
    assert "Get-SanitizedInnoLogTail" in invoke_setup
    assert "Get-SanitizedInnoLogTail" in invoke_expected_failure

    sanitizer = script[script.index("function Get-DiagnosticPathMasks") : script.index("function Invoke-Setup")]
    for expected in (
        "<REPOSITORY>",
        "<USERPROFILE>",
        "<LOCALAPPDATA>",
        "<TEMP>",
        "<TOKEN-RELATED-LINE-REDACTED>",
        "<PATH-RELATED-LINE-REDACTED>",
        "<SID>",
        "<SECRET>",
        "[ValidateRange(1, 80)]",
        "[int]$MaximumLines = 60",
        "$Protected.Length -gt 500",
    ):
        assert expected in sanitizer
    assert "Get-Content -LiteralPath $LogPath -Tail $MaximumLines" in sanitizer
    assert "return $Argument.Substring(5).Trim().Trim('\"')" in sanitizer

    profile_diagnostic = script[
        script.index("function Resolve-DiagnosticProfilePath") : script.index("function Wait-ServiceState")
    ]
    for expected in (
        "[Microsoft.Win32.RegistryHive]::LocalMachine",
        "[Microsoft.Win32.RegistryHive]::Users",
        "ProfileImagePath",
        '"S-1-5-18", "S-1-5-19", "S-1-5-20"',
        "loaded = 0",
        '"offline DAT" = 0',
        '"offline MAN" = 0',
        "missing = 0",
        "ambiguous = 0",
        "unsafe = 0",
        "ProfileList/Hive-Diagnose (nur Kategorien)",
    ):
        assert expected in profile_diagnostic
    assert "Write-Host $_" not in profile_diagnostic
    assert "Write-Warning $_" not in profile_diagnostic
    port_diagnostic = script[
        script.index("function Write-LoopbackPortCategoryDiagnostic") : script.index("function Wait-ServiceState")
    ]
    for expected in (
        'Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8765',
        "listen = 0",
        "timeWait = 0",
        "closeWait = 0",
        "other = 0",
        "TCP-Portdiagnose (nur Zähler)",
    ):
        assert expected in port_diagnostic
    for forbidden in (
        "OwningProcess",
        "RemoteAddress",
        "RemotePort",
        "Test-NetConnection",
        "TcpListener",
    ):
        assert forbidden not in port_diagnostic
    legitimate_service_install = script[
        script.index("$ServiceInstallArguments = @(") : script.index(
            'Wait-ServiceState -Name $ServiceName -State "Running"'
        )
    ]
    assert legitimate_service_install.index("Write-ProfileHiveCategoryDiagnostic") < (
        legitimate_service_install.index(
            'Invoke-Setup -Path $ServiceSetup -Scenario "Dienstinstallation nach Desktopdeinstallation"'
        )
    )
    assert legitimate_service_install.index("Write-LoopbackPortCategoryDiagnostic") < (
        legitimate_service_install.index(
            'Invoke-Setup -Path $ServiceSetup -Scenario "Dienstinstallation nach Desktopdeinstallation"'
        )
    )

    tree_fingerprint = script[
        script.index("function Get-TreeFingerprint") : script.index("function Convert-RegistryValueToStableText")
    ]
    for expected in (
        "Get-Item -LiteralPath $Root -Force",
        "Get-ChildItem -LiteralPath $Root -Recurse -Force",
        "[Security.AccessControl.AccessControlSections]::Owner",
        "[Security.AccessControl.AccessControlSections]::Access",
        "GetSecurityDescriptorSddlForm",
        "$Entry.PSIsContainer",
        "$Entry -is [IO.FileInfo]",
        "[int64]$Entry.Attributes",
        "Get-FileHash -LiteralPath $Entry.FullName -Algorithm SHA256",
        "$Lines.Sort([StringComparer]::Ordinal)",
    ):
        assert expected in tree_fingerprint
    assert "Get-ChildItem -LiteralPath $Root -Recurse -File" not in tree_fingerprint

    registry_fingerprint = script[
        script.index("function Convert-RegistryValueToStableText") : script.index("function Get-ServiceSnapshot")
    ]
    for expected in (
        "$Value -is [byte[]]",
        "$Value -is [string[]]",
        "$Value -is [IFormattable]",
        "[Globalization.CultureInfo]::InvariantCulture",
        "[Convert]::ToHexString([byte[]]$Value)",
        "Get-OptionalRegistryValue",
        "present|$Kind|$Value",
    ):
        assert expected in registry_fingerprint

    service_snapshot = script[
        script.index("function Get-ServiceSnapshot") : script.index("function Assert-ServiceSnapshotUnchanged")
    ]
    service_comparison = script[
        script.index("function Assert-ServiceSnapshotUnchanged") : script.index("if (-not $IsWindows)")
    ]
    for metadata in (
        "DisplayName",
        "Description",
        "DelayedAutoStart",
        "ServiceSidType",
        "FailureActions",
        "FailureActionsOnNonCrashFailures",
    ):
        assert f"{metadata} =" in service_snapshot
        assert f'"{metadata}"' in service_comparison
    for registry_metadata in (
        "ImagePath",
        "DisplayName",
        "Description",
        "ObjectName",
        "Start",
        "Type",
        "ErrorControl",
    ):
        assert f'-Name "{registry_metadata}"' in service_snapshot
    assert "[string]::Equals(" in service_comparison
    assert "[StringComparison]::Ordinal" in service_comparison

    rejected_service_residue_check = script[
        script.index("if ((Get-Service -Name $ServiceName") : script.index("Invoke-Setup -Path $DesktopUninstaller")
    ]
    assert "(Test-Path -LiteralPath $ServiceUninstallKey)" in rejected_service_residue_check


def test_mode_exclusion_test_covers_a_real_logged_off_profile_hive() -> None:
    script = _read("scripts/test_windows_mode_exclusion.ps1")

    for expected in (
        "ModeExclusionNativeProfileApi",
        "CreateProfile(",
        'EntryPoint = "CreateProfile"',
        'EntryPoint = "DeleteProfileW"',
        "[Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder profilePath",
        "$ProfilePathBuffer = [Text.StringBuilder]::new(260)",
        "New-LocalUser -Name $FixtureUserName",
        "Remove-LocalUser -SID $FixtureSidObject",
        '"ERPModeT$([Guid]::NewGuid()',
        '"ERPModeHive_$([Guid]::NewGuid()',
        '"load"',
        '"unload"',
        '"add"',
        '"delete"',
        "{D33FD9E5-0C5E-48ED-BF0C-E9D2962A45DF}_is1",
        '"InstallLocation"',
        '"DisplayVersion"',
        '"1.3.0"',
        '"Offline-Autostart-only eintragen"',
        '"Offline-v1.3-Uninstall-Key entfernen"',
        '"Offline-Autostart bereinigen"',
        "Assert-FirstDesktopPreflightRejected",
        "Assert-OfflineFixtureUnchanged",
        "Assert-ServiceInstallerFootprintAbsent",
        "Get-FileHash -LiteralPath $FixtureHivePath -Algorithm SHA256",
        "Get-TreeFingerprint -Path $FixtureCustomDesktopDir",
        "$FixtureProfileImagePath = Get-OptionalRegistryValue",
        '"Registry::HKEY_USERS\\$Sid"',
        '"Registry::HKEY_USERS\\$MountName"',
    ):
        assert expected in script
    assert "[Text.StringBuilder]::new(4096)" not in script

    first_preflight = script[
        script.index("function Assert-FirstDesktopPreflightRejected") : script.index(
            "function Assert-ServiceInstallerFootprintAbsent"
        )
    ]
    assert "Desktop-Gegenmodus profilübergreifend und read-only ausschließen" in first_preflight
    assert "Desktop-Gegenmodus unmittelbar vor der Diensttransition erneut ausschließen" in first_preflight
    assert '$Content.Contains("$FirstPreflight ist fehlgeschlagen")' in first_preflight
    assert "$Content.Contains($SecondPreflight)" in first_preflight

    fixture_start = script.index('$FixtureUserName = "ERPModeT')
    offline_uninstall_rejection = script.index(
        "Dienstinstallation bei offline registrierter benutzerdefinierter v1.3-Desktopinstallation"
    )
    offline_autostart_rejection = script.index(
        "Dienstinstallation bei Autostart-only in einem offline gehaltenen Profil"
    )
    clean_hive = script.index("$CleanOfflineHiveHash = (", offline_autostart_rejection)
    ordinary_desktop_install = script.index(
        'Invoke-Setup -Path $DesktopSetup -Scenario "Desktopinstallation"',
        fixture_start,
    )
    clean_service_install = script.index(
        'Invoke-Setup -Path $ServiceSetup -Scenario "Dienstinstallation nach Desktopdeinstallation"'
    )
    clean_hive_assertion = script.index(
        "-ExpectedHiveHash $CleanOfflineHiveHash",
        clean_service_install,
    )
    assert (
        fixture_start
        < offline_uninstall_rejection
        < offline_autostart_rejection
        < clean_hive
        < ordinary_desktop_install
        < clean_service_install
        < clean_hive_assertion
    )

    offline_uninstall_scenario = script[
        script.index("$OfflineDesktopUninstallKey = (") : script.index(
            'Invoke-RegistryTool -Scenario "Offline-v1.3-Hive für Autostarttest laden"'
        )
    ]
    assert '"InstallLocation"' in offline_uninstall_scenario
    assert '"DisplayVersion"' in offline_uninstall_scenario
    assert "Assert-FirstDesktopPreflightRejected" in offline_uninstall_scenario
    assert "Assert-ServiceInstallerFootprintAbsent" in offline_uninstall_scenario
    assert "-ExpectedHiveHash $OfflineUninstallHiveHash" in offline_uninstall_scenario

    offline_autostart_scenario = script[
        script.index('Invoke-RegistryTool -Scenario "Offline-v1.3-Hive für Autostarttest laden"') : clean_hive
    ]
    assert offline_autostart_scenario.index('"Offline-v1.3-Uninstall-Key entfernen"') < (
        offline_autostart_scenario.index('"Offline-Autostart-only eintragen"')
    )
    assert "Assert-FirstDesktopPreflightRejected" in offline_autostart_scenario
    assert "Assert-ServiceInstallerFootprintAbsent" in offline_autostart_scenario
    assert "-ExpectedHiveHash $OfflineAutostartHiveHash" in offline_autostart_scenario

    cleanup = script[script.index("} finally {") :]
    assert '"Eigene Offline-Hive-Mountbereinigung"' in cleanup
    assert "[ModeExclusionNativeProfileApi]::DeleteProfile(" in cleanup
    assert "Get-LocalUser -SID $FixtureSidObject" in cleanup
    assert "Remove-LocalUser -SID $FixtureSidObject" in cleanup
    assert "$FixtureCleanupProblems.Add(" in cleanup
    assert "if ($FixtureCleanupProblems.Count -gt 0)" in cleanup
    assert "Offline-Profilfixture wurde nicht rückstandsfrei bereinigt" in cleanup
    assert cleanup.count("} finally {") >= 2
    assert cleanup.index("} finally {", cleanup.index("} finally {") + 1) < cleanup.index(
        '"Eigene Offline-Hive-Mountbereinigung"'
    )
    assert "Profilrest konnte vor der Benutzerbereinigung nicht geprüft werden" in cleanup
    assert cleanup.index('"Eigene Offline-Hive-Mountbereinigung"') < cleanup.index(
        "[ModeExclusionNativeProfileApi]::DeleteProfile("
    )
    assert cleanup.index("[ModeExclusionNativeProfileApi]::DeleteProfile(") < cleanup.index(
        "Remove-LocalUser -SID $FixtureSidObject"
    )
    profile_delete = cleanup[
        cleanup.index("$CleanupProfileImagePath = Get-OptionalRegistryValue") : cleanup.index(
            "$FixtureProfileRemains = $true"
        )
    ]
    assert (
        profile_delete.index("Resolve-DiagnosticProfilePath")
        < profile_delete.index("[string]::Equals(")
        < profile_delete.index("[ModeExclusionNativeProfileApi]::DeleteProfile(")
    )
    assert "$FixtureSid,\n                    $null,\n                    $null" in profile_delete.replace("\r\n", "\n")
    assert profile_delete.index("$DeleteProfileError = if ($ProfileDeleted)") < profile_delete.index(
        "Test-Path -LiteralPath $FixtureProfileListPath"
    )
    assert "Remove-Item -LiteralPath $FixtureProfilePath -Recurse" not in cleanup
