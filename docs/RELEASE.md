# Release-Prozess

## 1. Version vorbereiten

Version in folgenden Dateien ändern:

- `VERSION`
- `pyproject.toml`
- `app/__init__.py`
- `USER_AGENT` in `scripts/install_kosit.py`
- Versionskopf in `START_HERE.txt`; außerdem den gesamten Einstiegstext auf releasebezogene Beispiele und
  weiterhin zutreffende Upgradehinweise prüfen

Anschließend die Änderungen in `CHANGELOG.md` aus „Unveröffentlicht“ unter eine datierte Überschrift
`## <Version> – JJJJ-MM-TT` verschieben; ein leerer Abschnitt „Unveröffentlicht“ bleibt darüber bestehen. Bei
einem Breaking Release müssen Upgradehinweis, betroffene Consumer und die Migrationsanleitung ausdrücklich
genannt sein.

```sh
python scripts/verify_version.py
```

Vor dem Vorab-Workflow und dem Tag muss `git status --short` ausschließlich die beabsichtigten Änderungen
zeigen. Alle releasekritischen Dateien – insbesondere neue Laufzeitdaten, Sperrdateien, Migrationsdokumente und
Tests – müssen im zu taggenden Commit enthalten sein; ein lokaler Build aus ungetrackten Dateien ist kein
Nachweis für den Inhalt des späteren Tag-Builds.

## 2. Qualitätsprüfung

```sh
./scripts/check.sh
python -m pip_audit --strict .
python -m pip_audit --strict --disable-pip --require-hashes \
  -r packaging/windows/requirements-release.txt
```

Der Projektmodus prüft die in `pyproject.toml` deklarierten Fremdabhängigkeiten, ohne das lokal editierbar
installierte und nicht auf PyPI veröffentlichte Projekt selbst als externe Distribution zu behandeln.

Zusätzlich die Anwendung mit den anonymisierten CII-/UBL-Beispielen und einer Hybrid-PDF manuell öffnen. Bei KoSIT-Änderungen mindestens einen Annahme-, Ablehnungs- und Startfehlerfall prüfen.

### Analyseschema-2-Gate

Analyseschema 2 ist ein sofortiger Breaking Change am bestehenden Endpunkt. Vor einem Release müssen Server,
Browseroberfläche, Berichtsrenderer und alle mitgelieferten Automatisierungsconsumer atomar auf Schema 2 stehen.
Es darf kein Legacy-Endpunkt, Versionsparameter oder Adapter für Schema 1 in ein Artefakt gelangen.

Mindestens ausführen:

```sh
python -m pytest \
  tests/test_api_v2.py \
  tests/test_assessment_contract.py \
  tests/test_analysis_schema_v2.py \
  tests/test_document_types.py \
  tests/test_document_semantics.py \
  tests/test_payment_validation.py \
  tests/test_profiles.py \
  tests/test_kosit.py
```

Die Abnahme muss zusätzlich bestätigen:

- `POST /api/analyze` liefert ausschließlich `schema_version == 2`;
- HTML und PDF liefern alle sechs Schema-2-Header einschließlich `X-Einvoice-Report-Scope` und keinen der
  früheren Statusheader;
- offizielle, interne und technische Achse werden nicht zu einem Sammelstatus verdichtet;
- BG-/BT-Codes stehen unter `semantic_references`, nicht in `xml_location`;
- unbekannte/fehlende Dokumenttypen und unpassende UBL-Roots werden nicht als Standardrechnung geraten;
- Kartenkennungen sind in strukturiertem Ergebnis und technischen Textansichten maskiert, während `/api/xml`
  weiterhin bytegetreu bleibt.

Die Release Notes und `CHANGELOG.md` müssen den inkompatiblen Vertrag und
[`API_MIGRATION_V2.md`](API_MIGRATION_V2.md) ausdrücklich nennen. Eine bloße Aktualisierung des Servers ohne
gleichzeitige Consumer-Migration ist kein unterstützter Upgradepfad.

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

Wheel, Source Distribution und Repository-ZIP müssen außerdem alle zur Laufzeit beziehungsweise zur
dokumentierten Einrichtung benötigten Metadaten enthalten. Insbesondere sind
`app/presentation_contract.json`, `packaging/kosit/components.lock.json` und
`docs/examples/node-red-e-rechnungs-pruefer-flow.json` stichprobenartig im jeweils vorgesehenen Artefakt zu
kontrollieren.

### Windows-x64-Installer

Der Windows-Build läuft nativ auf Windows und ist in [`WINDOWS_PACKAGE.md`](WINDOWS_PACKAGE.md) beschrieben. Er
erzeugt den benutzerbezogenen Desktop-/Tray-Installer und den administrativen Dienst-Installer aus derselben
geprüften Codebasis:

```powershell
python scripts\prepare_windows_components.py
.\scripts\build_windows.ps1 -BuildElevatedRecoveryTestInstaller
.\scripts\test_windows_package.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_mode_exclusion.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_service_package.ps1 -ConfirmIsolatedEnvironment `
    -AllowElevatedRecoveryTestContext -CommitHardKillRecovery Immediate
```

Die signierten GitHub-Builds verwenden exakt CPython 3.13.14 und installieren sämtliche Laufzeit-, Test- und
Buildabhängigkeiten ausschließlich aus `packaging/windows/requirements-release.txt`. Dort sind alle Pakete samt
transitiven Abhängigkeiten auf die ausgewählten Windows-x64-Wheels und deren SHA-256-Hashes festgelegt. Dadurch
verwenden manueller Probelauf und späterer Tag-Lauf dieselbe Python-Abhängigkeitsbasis. Der allgemeinere
Kompatibilitätstest in `ci.yml` prüft weiterhin die unterstützten Python-Versionen und zulässigen
Abhängigkeitsbereiche.

Im signierten Vorab-Probelauf werden sämtliche Paket-, Modusausschluss- und Recoverytest-Aufrufe zusätzlich mit
`-RequireSignature` ausgeführt.

Die Pakettests verwenden die echten Produkt-IDs, Dienstnamen, Registry- und Laufzeitpfade. Sie dürfen deshalb nur
in einer sauberen, entbehrlichen Windows-VM oder unter einer eigenen Testidentität laufen.
`-ConfirmIsolatedEnvironment` bestätigt diese Voraussetzung; die Skripte brechen trotzdem vor Änderungen ab,
wenn sie fremden oder vorhandenen Produktzustand finden. Auf einer regulär genutzten Identität könnten die Tests
Installationen, API-Token, Autostart oder Dienstzustände verändern. Unvollständige v1.4.0-Migrations-, Transfer-,
Seal-, Quarantäne- oder kombinierte Alttransaktionszustände werden nicht unterstützt; die Test-VM muss auch davon
frei sein. Der Modusausschlusstest prüft beide Installationsrichtungen ohne Tokenübernahme und zusätzlich, dass
reines, bei einer Dienstdeinstallation erhaltenes ProgramData den Desktopmodus nicht blockiert oder verändert.
Der zusätzlich unter `build\windows\test-installer` erzeugte und signierte VM-Recovery-Testinstaller ist
präprozessorseitig der einzige Build, der `/ALLOWELEVATEDTESTCONTEXT=1` unterstützt. Er wird weder nach `dist`
noch in das normale Windows-Artefakt oder einen GitHub Release übernommen; der produktive Dienst-Installer in
`dist` enthält diesen Testpfad nicht. Nur ein manueller signierter Vorab-Probelauf auf `main` stellt ihn für
einen Tag als separates internes Actions-Artefakt bereit.
Der opt-in Hard-Kill-Lauf erkennt seinen service-only Commit-Checkpoint nur über vollständig geparste, DACL- und
Transaktions-ID-geprüfte persistente Marker und beendet ausschließlich den exakt von ihm gestarteten
Setup-Prozessbaum. Ein nicht eindeutig erreichter Checkpoint oder ein anderer als der ausdrücklich angeforderte
transaktionale Folgefehler ist ein fehlgeschlagener, nicht etwa ein übersprungener oder bestandener Test.

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

Ein reduzierter Build mit `-WithoutOfficialValidation` ist nur ein Entwicklungsartefakt. Vor einem
Endbenutzerrelease müssen Java, KoSIT und XRechnung gemäß
[`packaging/windows/components.lock.json`](../packaging/windows/components.lock.json) eingebunden und durch den
installierten Pakettest ausgeführt worden sein. Die KoSIT- und XRechnung-Einträge dieses Windows-Locks müssen
den beiden Einträgen des nachfolgend genannten zentralen KoSIT-Locks entsprechen; nur der Windows-Lock ergänzt
die Java-Laufzeit.

Für KoSIT und XRechnung ist
[`packaging/kosit/components.lock.json`](../packaging/kosit/components.lock.json) die maßgebliche Sperrdatei:

| Komponente | Festgelegter Stand | SHA-256 |
|---|---|---|
| KoSIT Validator | `validator-1.6.2-standalone.jar` / 1.6.2 | `244978514ad48f67c7573acfffc8f4fd73d81feda6f276710033f9913579857e` |
| XRechnung-Konfiguration | `xrechnung-3.0.2-validator-configuration-2026-01-31.zip` | `6a5a5911a421b25fbc423f62f93f894df7b236f5d73ca4f84bb222a945082704` |

Die darin ausgewiesenen Standards sind XRechnung 3.0.2, Konfigurationsstand 2026-01-31,
CEN-EN-16931-Regeln 1.3.15 und XRechnung-Schematron 2.5.0. Dateiname, URL, Version und Hash müssen gemeinsam
aktualisiert werden; `app/component_versions.py`, Health-Antwort, Tests und Dokumenttyp-Registry müssen denselben
Stand nennen. Ein Hash- oder Versionsunterschied ist ein Releasefehler.

## 4. Artefakte prüfen

```sh
unzip -l dist/E-Rechnungs-Pruefer-*-Codex-GitHub.zip
python -m twine check dist/*.whl dist/*.tar.gz
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
stellt die Produktionsdateien für 14 Tage als Actions-Artefakt
`windows-release-<Run-ID>-<Versuch>` bereit. Zusätzlich
enthält ausschließlich dieser manuelle Lauf das separate Artefakt
`INTERNAL-TEST-windows-recovery-<Run-ID>-<Versuch>` mit dem signierten internen Recovery-Testinstaller; es wird
nur einen Tag aufbewahrt. Das interne Artefakt wird niemals von Tag-Läufen hochgeladen oder an einen GitHub
Release angehängt. Der manuelle Workflowlauf veröffentlicht keinen GitHub Release. Beide Artefakte sind in
einem öffentlichen Repository nicht vertraulich und können von angemeldeten GitHub-Nutzern mit
Repository-Lesezugriff heruntergeladen werden.

Der interne Recovery-Testinstaller enthält den ausschließlich für isolierte Wegwerf-VMs vorgesehenen erhöhten
Testkontext `/ALLOWELEVATEDTESTCONTEXT=1` und darf nicht als Produktinstaller verwendet werden. Für die
persistente Hard-Kill-Abnahme ist ein Checkout exakt des im Workflowlauf genannten Commits erforderlich. Aus dem
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

Das signierte Vorab-Artefakt ist anschließend anhand der folgenden verbindlichen Matrix zu prüfen. Jeder Lauf
beginnt auf einem dokumentierten, sauberen VM-Snapshot; ein offenes Alt-Tab bleibt bei den Upgradefällen bewusst
über das Update hinweg geöffnet.

| System | Betriebsart | Ausgangsstand | Pflichtschwerpunkt |
|---|---|---|---|
| Windows 10 x64 | Desktop | 1.5.0 → Zielversion | warmer Browsercache, offenes Alt-Tab, CII/UBL mit KoSIT |
| Windows 10 x64 | Desktop | 2.0.1 → Zielversion | realer Patch-Upgradepfad und UI-Revisionskonflikt |
| Windows 10 x64 | Dienst | 1.5.0 → Zielversion | warmer Cache, Öffnen-Client, CII/UBL mit KoSIT |
| Windows 10 x64 | Dienst | 2.0.1 → Zielversion | Erhalt von Dienstkonfiguration und API-Token |
| Windows 10 x64 | Desktop und Dienst getrennt | Neuinstallation | CII, UBL und Hybrid-PDF |
| Windows 11 x64 | Desktop und Dienst | Zielversion | vollständige Installer-, Ausschluss- und Recoveryabnahme |
| Windows Server 2022 | CI | Zielversion | automatisierter Paket- und Integrationstest |

Ein altes Tab muss eine kontrollierte Aufforderung zum Schließen und erneuten Öffnen anzeigen: nach einem
Prozessneustart als `403 desktop_session_error` wegen der absichtlich ungültigen alten Sitzung, innerhalb einer
noch gültigen Sitzung mit abweichender Oberfläche als `409 ui_version_mismatch`. Ein neues, über Launcher oder
Öffnen-Client gestartetes Fenster muss unmittelbar die aktuelle Oberfläche laden. Ein JavaScript-Stackfehler
wie `Cannot set properties of null` ist ein Releaseblocker.

Neben den automatisierten Paket-, Modusausschluss- und Recoverytests umfasst die manuelle Abnahme:

1. Bundle-ZIP entpacken und Signaturen sowie SHA-256-Prüfsummen aller fünf eigenen Dateien und des ZIPs prüfen;
2. Desktopstart, Tray, Standardbrowser und HKCU-Autostart, danach reguläre Desktopdeinstallation;
3. auf einer echten zweiten lokalen Testidentität die signierte v1.3.0-Desktopversion in einem benutzerdefinierten
   Zielordner mit Autostart installieren, die Identität abmelden und bestätigen, dass ihr Benutzerhive nicht mehr
   unter `HKEY_USERS` geladen ist; das aus der administrativen Testidentität gestartete Dienstsetup muss
   fail-closed abbrechen und Desktopinstallation, Hive-Datei, Token und Autostart im abgemeldeten Profil
   unverändert lassen. Danach den Desktop unter der zweiten Identität regulär deinstallieren und wieder abmelden;
4. Dienstkonto, Service-SID, DACLs, Starttyp, Recovery und Öffnen-Client;
5. tatsächlichen Windows-Neustart und erfolgreichen verzögerten Dienststart vor der ersten Benutzeranmeldung;
6. API ohne, mit falschem und mit richtigem Token, `schema_version == 2`, alle sechs neuen Berichtsheader,
   `scope=readable|complete`, PDF-Bericht, bytegetreuen XML-Export sowie echte KoSIT-Annahme und -Ablehnung; die
   früheren Header
   `X-Einvoice-Validation-Status` und `X-Einvoice-Official-Status` dürfen nicht erscheinen;
7. In Microsoft Edge über eine RDP-Sitzung auf einer ressourcenarmen VM die offiziellen CII- und
   UBL-Beispielprüfungen mit dem unveränderten Standardtimeout von 60 Sekunden ausführen. Als API-only-Kontrolle
   dieselben Prüfungen ohne geöffnete Browseroberfläche ausführen und die Laufzeiten gegenüberstellen. Der Browser
   darf während des sichtbaren Ladezustands die CPU nicht dauerhaft sättigen; eine kurzzeitig hohe CPU-Nutzung des
   Java-Prozesses während der KoSIT-Ausführung ist dagegen erwartet.
8. Update eines laufenden und eines gestoppten Dienstes, automatisierten Fehler-Rollback, tatsächlichen
   Recovery-Neustart sowie Deinstallation mit erhaltenem Maschinenzustand und mit ausdrücklicher vollständiger
   Löschung;
9. den gegenseitigen Installationsausschluss sowie den Preserve-Fall prüfen: Dienst ohne Datenlöschung
   deinstallieren, Desktopmodus bei unverändertem ProgramData installieren und entfernen, danach den Dienst mit
   demselben Maschinentoken erneut installieren;
10. auf einem sauberen Snapshot die persistente service-only Installer-Recovery mit
   `-CommitHardKillRecovery LeaveForReboot` vorbereiten, Exitcode `194` als absichtlich unvollständigen Lauf
   dokumentieren, die VM hart neu starten und denselben Testinstaller erneut ausführen; anschließend Roll-forward
   sowie die vollständige Marker- und Bundlebereinigung nachweisen;
11. bei gefordertem Betrieb vor Anmeldung auch den vollständigen Node-RED-Ablauf, wobei Node-RED selbst als
   Dienst unter der vorgesehenen Identität laufen muss.

Erst nach dokumentiert bestandenem Vorab-Probelauf, manueller Windows-Abnahme und ausdrücklicher Freigabe darf
der Tag erzeugt werden. Das öffentliche Release bleibt bis zur nachfolgenden Prüfung der taggenauen Artefakte
gesperrt.

## 5. Tag und GitHub Release

```sh
git tag -a vX.Y.Z -m "E-Rechnungs-Pruefer X.Y.Z"
git push origin vX.Y.Z
```

Der Release-Workflow wiederholt Check und Build, verifiziert die Tag-Version und lädt die exakt in diesem
Tag-Lauf erzeugten Dateien in einen **Draft**. Er veröffentlicht diesen Draft ausdrücklich nicht automatisch.
Fehlen Azure-Anmeldung, Key-Vault-Konfiguration, gültige Signatur oder eines der erwarteten Artefakte, schlägt
der Lauf fehl. Vor der manuellen Veröffentlichung werden genau die Dateien des Drafts heruntergeladen und
mindestens fokussiert auf Windows 10 geprüft. Vorab- und Tag-Build können wegen Build- und
Signaturzeitstempeln trotz identischem Commit verschiedene Bytes haben; ein bestandener Vorabtest ersetzt daher
nicht diese letzte Prüfung.

Zu protokollieren sind Tag, Commit-SHA, Workflow-Run-ID und -Versuch, Installer-SHA-256, Windows-Build,
Edge-Version und Testergebnis. Erst danach darf der bestehende Draft manuell veröffentlicht werden. Dateien
werden unter einem einmal veröffentlichten Release niemals ersetzt.

Artefakte werden zwischen den Jobs über ihre unveränderlichen GitHub-Artefakt-IDs übergeben, sodass auch ein
partieller Rerun erfolgreiche Dateien aus dem vorherigen Versuch eindeutig weiterverwendet. Existiert nach
einem unterbrochenen Publish-Job bereits ein Draft, akzeptiert der Folgelauf vorhandene Dateien nur
byteidentisch, ergänzt ausschließlich fehlende Dateien und bricht bei abweichenden oder unerwarteten Assets
geschlossen ab. Ein bereits veröffentlichtes Release wird niemals durch den Workflow verändert.

## Rücknahme

Bei einem fehlerhaften Release keine vorhandenen Artefakte still ersetzen. Release als fehlerhaft kennzeichnen, neuen Patch erstellen und im Changelog transparent beschreiben.
