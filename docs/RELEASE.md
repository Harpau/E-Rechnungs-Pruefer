# Release-Prozess

## 1. Version vorbereiten

Version in folgenden Dateien ändern:

- `VERSION`
- `pyproject.toml`
- `app/__init__.py`
- `USER_AGENT` in `scripts/install_kosit.py`

Anschließend `CHANGELOG.md` aktualisieren.

```sh
python scripts/verify_version.py
```

## 2. Qualitätsprüfung

```sh
./scripts/check.sh
python -m pip_audit --strict .
```

Der Projektmodus prüft die in `pyproject.toml` deklarierten Fremdabhängigkeiten, ohne das lokal editierbar
installierte und nicht auf PyPI veröffentlichte Projekt selbst als externe Distribution zu behandeln.

Zusätzlich die Anwendung mit den anonymisierten CII-/UBL-Beispielen und einer Hybrid-PDF manuell öffnen. Bei KoSIT-Änderungen mindestens einen Annahme-, Ablehnungs- und Startfehlerfall prüfen.

## 3. Artefakte bauen

```sh
python scripts/build_release.py
```

`dist/` enthält:

- `E-Rechnungs-Pruefer-<Version>-Codex-GitHub.zip`
- Wheel
- Source Distribution
- `E-Rechnungs-Pruefer-<Version>-SHA256SUMS.txt`

Der Repository-Build schließt `.git`, virtuelle Umgebungen, lokale `.env`-Dateien, KoSIT-Dateien, gebündelte Java-Laufzeiten, Download-Caches, PDFs, Schlüsselmaterial, Berichte und nicht freigegebene XML-Dateien aus.

### Windows-x64-Installer

Der Windows-Build läuft nativ auf Windows und ist in [`WINDOWS_PACKAGE.md`](WINDOWS_PACKAGE.md) beschrieben. Er
erzeugt den benutzerbezogenen Desktop-/Tray-Installer und den administrativen Dienst-Installer aus derselben
geprüften Codebasis:

```powershell
python scripts\prepare_windows_components.py
.\scripts\build_windows.ps1 -BuildElevatedMigrationTestInstaller
.\scripts\test_windows_package.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_service_package.ps1 -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext
.\scripts\test_windows_migration.ps1 -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext
.\scripts\test_windows_service_package.ps1 -ConfirmIsolatedEnvironment `
    -AllowElevatedMigrationTestContext -CommitHardKillRecovery Immediate
.\scripts\test_windows_migration.ps1 -ConfirmIsolatedEnvironment `
    -AllowElevatedMigrationTestContext -DesktopHardKillRecovery Immediate
```

Die signierten GitHub-Builds verwenden exakt CPython 3.13.14 und installieren sämtliche Laufzeit-, Test- und
Buildabhängigkeiten ausschließlich aus `packaging/windows/requirements-release.txt`. Dort sind alle Pakete samt
transitiven Abhängigkeiten auf die ausgewählten Windows-x64-Wheels und deren SHA-256-Hashes festgelegt. Dadurch
verwenden manueller Probelauf und späterer Tag-Lauf dieselbe Python-Abhängigkeitsbasis. Der allgemeinere
Kompatibilitätstest in `ci.yml` prüft weiterhin die unterstützten Python-Versionen und zulässigen
Abhängigkeitsbereiche.

Im signierten Vorab-Probelauf werden sämtliche Paket- und Migrationstest-Aufrufe zusätzlich mit
`-RequireSignature` ausgeführt.

Die Pakettests verwenden die echten Produkt-IDs, Dienstnamen, Registry- und Laufzeitpfade. Sie dürfen deshalb nur
in einer sauberen, entbehrlichen Windows-VM oder unter einer eigenen Testidentität laufen.
`-ConfirmIsolatedEnvironment` bestätigt diese Voraussetzung; die Skripte brechen trotzdem vor Änderungen ab,
wenn sie fremden oder vorhandenen Produktzustand finden. Auf einer regulär genutzten Identität könnten die Tests
Installationen, API-Token, Autostart oder Dienstzustände verändern. Der Migrationstest installiert ausdrücklich den
veröffentlichten, signierten Desktopstand v1.3.0 und wechselt mit Token-Opt-in zum neuen Dienstmodus.
Der dafür zusätzlich unter `build\windows\test-installer` erzeugte und signierte VM-Testinstaller ist
präprozessorseitig der einzige Build, der `/ALLOWELEVATEDTESTCONTEXT=1` unterstützt. Er wird weder nach `dist`
noch in das normale Windows-Artefakt oder einen GitHub Release übernommen; der produktive Dienst-Installer in
`dist` enthält diesen Testpfad nicht. Nur ein manueller signierter Vorab-Probelauf auf `main` stellt ihn für
einen Tag als separates internes Actions-Artefakt bereit.
Die beiden opt-in Hard-Kill-Läufe erkennen ihren Checkpoint nur über vollständig geparste, DACL- und
Transaktions-ID-geprüfte persistente Marker. Der Desktop-Helfer hält seinen internen Testcheckpoint zusätzlich
über eine ausschließlich im isolierten Testinstaller enthaltene, vom Testprozess kontrollierte Haltesperre
stabil und signalisiert ihn erst nach vollständig abgeschlossener Desktop-Verifikation. Beide Helfer beenden
ausschließlich den exakt von ihnen gestarteten Setup-Prozessbaum. Ein nicht eindeutig erreichter Checkpoint oder
ein anderer als der ausdrücklich angeforderte transaktionale Folgefehler ist ein fehlgeschlagener, nicht etwa ein
übersprungener oder bestandener Test.

Zusätzliche Artefakte:

- `E-Rechnungs-Pruefer-<Version>-Windows-x64-Setup.exe`
- `E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe`
- `E-Rechnungs-Pruefer-<Version>-Windows-x64-Binaries.zip`
- `E-Rechnungs-Pruefer-<Version>-Windows-x64-SHA256SUMS.txt`

Vor der Prüfsummenerzeugung werden genau die drei eigenen Programme
`E-Rechnungs-Pruefer.exe`, `E-Rechnungs-Pruefer-Dienst.exe` und
`E-Rechnungs-Pruefer-Oeffnen.exe` sowie beide Installer signiert und unmittelbar verifiziert. Das anschließend
erzeugte ZIP enthält die vollständigen signierten Bundles in den Pfaden, die das Prüfsummenmanifest nennt. Die
gemeinsame SHA-256-Datei enthält alle fünf signierten Dateien und das ZIP selbst. Nach dem Entpacken des ZIPs neben
die beiden Installer kann das Manifest vollständig geprüft werden. Gebündelte Drittprogramme wie Java erhalten keine
Projektsignatur.

Ein reduzierter Build mit `-WithoutOfficialValidation` ist nur ein Entwicklungsartefakt. Vor einem Endbenutzerrelease müssen Java, KoSIT und XRechnung aus `components.lock.json` eingebunden und durch den installierten Pakettest ausgeführt worden sein.

## 4. Artefakte prüfen

```sh
unzip -l dist/E-Rechnungs-Pruefer-*-Codex-GitHub.zip
python -m twine check dist/*
```

Empfohlen ist außerdem ein Installationstest in einer neuen virtuellen Umgebung:

```sh
python -m venv /tmp/einvoice-release-test
/tmp/einvoice-release-test/bin/python -m pip install dist/*.whl
/tmp/einvoice-release-test/bin/python -c "import app; print(app.__version__)"
```

### Signierter Windows-Vorab-Probelauf

Vor einem öffentlichen Tag wird der Workflow `Release` manuell auf `main` gestartet. Dieser Lauf verwendet die
geschützte Umgebung `release`, signiert alle eigenen Windows-EXEs und beide Installer über Azure Key Vault und
stellt die Produktionsdateien für drei Tage als Actions-Artefakt `windows-release-<Run-ID>` bereit. Zusätzlich
enthält ausschließlich dieser manuelle Lauf das separate Artefakt
`INTERNAL-TEST-windows-recovery-<Run-ID>-<Versuch>` mit dem signierten internen Recovery-Testinstaller; es wird
nur einen Tag aufbewahrt. Das interne Artefakt wird niemals von Tag-Läufen hochgeladen oder an einen GitHub
Release angehängt. Der manuelle Workflowlauf veröffentlicht keinen GitHub Release. Beide Artefakte sind in
einem öffentlichen Repository nicht vertraulich und können von angemeldeten GitHub-Nutzern mit
Repository-Lesezugriff heruntergeladen werden.

Der interne Recovery-Testinstaller enthält den ausschließlich für isolierte Wegwerf-VMs vorgesehenen erhöhten
Testkontext `/ALLOWELEVATEDTESTCONTEXT=1` und darf nicht als Produktinstaller verwendet werden. Für die
persistenten Hard-Kill-Tests ist ein Checkout exakt des im Workflowlauf genannten Commits erforderlich. Aus dem
normalen Artefakt wird das Binär-ZIP zunächst in ein leeres Zwischenverzeichnis entpackt. Dessen Inhalt
`bundle\desktop\*` wird anschließend nach `build\windows\bundle\E-Rechnungs-Pruefer\*` kopiert, sodass die
Desktop-EXE exakt unter `build\windows\bundle\E-Rechnungs-Pruefer\E-Rechnungs-Pruefer.exe` liegt; ein direktes
Entpacken nach `build\windows` genügt wegen der absichtlich neutralen veröffentlichten ZIP-Pfade nicht. Der
produktive Dienst-Installer wird unter
`dist\E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe` abgelegt. Die aus dem internen Artefakt geladene,
gleichnamige Test-EXE muss dagegen ausschließlich unter
`build\windows\test-installer\E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe` liegen. Diese
Trennung ist fail-closed in den Testskripten verankert. Vor der Verwendung sind bei beiden Installern
Authenticode-Status und Zeitstempel erneut zu prüfen.

Das signierte Artefakt ist anschließend auf einer sauberen, nach dem Test verworfenen Windows-11-x64-VM zu
prüfen. Neben beiden automatisierten Pakettests und dem v1.3.0-Migrationstest umfasst die manuelle Abnahme:

1. Bundle-ZIP entpacken und Signaturen sowie SHA-256-Prüfsummen aller fünf eigenen Dateien und des ZIPs prüfen;
2. Desktopstart, Tray, Standardbrowser und HKCU-Autostart;
3. Dienstkonto, Service-SID, DACLs, Starttyp, Recovery und Öffnen-Client;
4. tatsächlichen Windows-Neustart und erfolgreichen verzögerten Dienststart vor der ersten Benutzeranmeldung;
5. API ohne, mit falschem und mit richtigem Token, PDF-Bericht, bytegetreuen XML-Export sowie echte
   KoSIT-Annahme und -Ablehnung;
6. Update eines laufenden und eines gestoppten Dienstes, automatisierten Fehler-Rollback, tatsächlichen
   Recovery-Neustart sowie Deinstallation mit erhaltenem Maschinenzustand und mit ausdrücklicher vollständiger
   Löschung;
7. auf getrennten sauberen Snapshots beide persistenten Installer-Recovery-Richtungen mit
   `-DesktopHardKillRecovery LeaveForReboot` beziehungsweise
   `-CommitHardKillRecovery LeaveForReboot` vorbereiten, Exitcode `194` als absichtlich unvollständigen Lauf
   dokumentieren, die VM jeweils hart neu starten und denselben Testinstaller erneut ausführen; anschließend
   Rollback beziehungsweise Roll-forward sowie die vollständige Marker- und Bundlebereinigung nachweisen;
8. bei gefordertem Betrieb vor Anmeldung auch den vollständigen Node-RED-Ablauf, wobei Node-RED selbst als
   Dienst unter der vorgesehenen Identität laufen muss.

Erst nach dokumentiert bestandenem Vorab-Probelauf, manueller Windows-Abnahme und ausdrücklicher Freigabe
dürfen Tag und öffentliches Release erzeugt werden.

## 5. Tag und GitHub Release

```sh
git tag -a vX.Y.Z -m "E-Rechnungs-Pruefer X.Y.Z"
git push origin vX.Y.Z
```

Der Release-Workflow wiederholt Check und Build, verifiziert die Tag-Version und veröffentlicht die Dateien aus
`dist/`. Beide Windows-Installer werden nur dann an den öffentlichen GitHub Release angehängt, wenn alle
vorgesehenen Authenticode-Signaturen gültig sind und die Windows-Paketprüfungen bestanden wurden. Fehlen
Azure-Anmeldung, Key-Vault-Konfiguration, gültige Signatur oder eines der erwarteten Artefakte, schlägt der
Release fehl. Der Publish-Job legt zunächst einen Draft an, lädt den vollständigen Artefaktsatz hoch und
veröffentlicht den Draft erst danach; damit ist der Ablauf mit unveränderlichen GitHub Releases kompatibel.

## Rücknahme

Bei einem fehlerhaften Release keine vorhandenen Artefakte still ersetzen. Release als fehlerhaft kennzeichnen, neuen Patch erstellen und im Changelog transparent beschreiben.
