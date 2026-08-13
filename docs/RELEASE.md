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

Die anonymisierten CII-/UBL-Beispiele und die Hybrid-PDF sind durch die Regressionstests abgedeckt. Eine
zusätzliche manuelle Sichtprüfung ist nur erforderlich, wenn eine Änderung ein visuelles Verhalten einführt,
das nicht sinnvoll automatisiert prüfbar ist, oder wenn ein konkreter automatisierter Befund eine Sichtprüfung
erfordert. Reine Parser- und Renderingänderungen mit ausreichenden Regressionstests lösen keine pauschale
Drei-Formate-Sichtprüfung aus. Dasselbe gilt für KoSIT-Annahme, -Ablehnung und technische Startfehler: Die
automatisierten Fälle genügen, solange keine neue, nur visuell beurteilbare Darstellung betroffen ist.

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
$InnoSetupCompiler = .\scripts\install_inno_setup.ps1
.\scripts\build_windows.ps1 -InnoSetupCompiler $InnoSetupCompiler -BuildElevatedRecoveryTestInstaller
.\scripts\test_windows_package.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_mode_exclusion.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_service_package.ps1 -ConfirmIsolatedEnvironment `
    -AllowElevatedRecoveryTestContext -CommitHardKillRecovery Immediate
```

Die CI- und signierten GitHub-Builds installieren zusätzlich den offiziellen Inno-Setup-7.0.2-x64-Compiler aus
seinem unveränderlichen Releaseasset, prüfen Installer und Compiler gegen die festgeschriebenen SHA-256-Werte
und übergeben ausschließlich diesen Compilerpfad an den Windows-Build. Die signierten GitHub-Builds verwenden
außerdem exakt CPython 3.13.14 und installieren sämtliche Laufzeit-, Test- und Buildabhängigkeiten ausschließlich
aus `packaging/windows/requirements-release.txt`. Dort sind alle Pakete samt
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

### Risikobasierte manuelle Windows-Abnahme

Der signierte Vorab-Probelauf auf Windows Server 2022 prüft mit dem Produktions-Desktopinstaller den vollständigen
Desktop-Paketpfad und mit dem Produktions-Dienstinstaller Signatur, Inventar und Modusausschluss. Der vollständige
Dienst-, Update-, Rollback- und Immediate-Recovery-Harness verwendet dagegen bewusst den getrennten internen
Recovery-Testinstaller. Zusammen decken diese Läufe Neuinstallation und Update, API/PDF/XML und KoSIT, Cache-
und UI-Revisionsvertrag, Modusausschluss, Rollback, Preserve/Purge sowie die unmittelbare Hard-Kill-Recovery ab.
Diese technischen Fälle werden auf den Clientbetriebssystemen nicht manuell wiederholt. Dort verbleiben nur
Client-OS-, echte Browser- und historische Upgrade-Risiken, die der GitHub-Runner nicht überzeugend abdeckt.

Windows 10 22H2 x64 im Home/Pro-Kanal hat laut
[Microsoft-Lifecycle](https://learn.microsoft.com/de-de/lifecycle/products/windows-10-home-and-pro) das Ende
des regulären Supports erreicht und wird nur noch als **Best-Effort-Kompatibilität** geprüft, nicht als
vollständig abgenommene Freigabeplattform. LTSC- und ESU-Konstellationen besitzen eigene Lebenszyklen und sind
durch diesen Kompatibilitätslauf nicht abgedeckt.

Die verbindliche Matrix lautet:

| System | Betriebsart | Ausgangsstand | Pflichtnachweis |
|---|---|---|---|
| Windows Server 2022 | automatisierte Paketpfade | Zielversion | Produktions-Desktop, Produktions-Dienst-Modusausschluss und vollständiger interner Dienst-/Immediate-Recovery-Harness |
| Windows 10 22H2 x64 | Desktop und Dienst, durch Snapshot getrennt | Neuinstallation | Desktop-Long-Path-, Start- und KoSIT-Smoke; Dienst-Installations-, Health- und Öffnen-Client-Smoke |
| Windows 11 x64 | Desktop | vorherige Patchversion → Zielversion | warmer Browsercache, offenes Alt-Tab, kontrollierter UI-Revisionswechsel und ein sichtbarer UBL-KoSIT-Lauf |
| Windows 11 x64 | Dienst | vorherige Patchversion → Zielversion | Erhalt von Dienstkonfiguration und API-Token, laufender Dienst und sichtbarer Öffnen-Client |

Jeder manuelle Lauf beginnt auf einem dokumentierten, sauberen VM-Snapshot. Vor der ersten Produktmutation sind
Commit, Workflow-Run und -Versuch sowie SHA-256 und Authenticode-Signatur des verwendeten Installers an die
Abnahmeevidenz zu binden.

Der Windows-10-Kompatibilitätslauf bleibt bewusst klein:

1. Auf dem Desktop-Snapshot bleibt `LongPathsEnabled=0`; der Zustand wird protokolliert. Der signierte
   Desktopinstaller wird mit einem benutzerdefinierten `/DIR`-Ziel installiert, bei dem der vollständige Pfad von
   `_internal\vendor\kosit\xrechnung\resources\cii\16b\xsd\CrossIndustryInvoice_ReusableAggregateBusinessInformationEntity_100pD16B.xsd`
   nachweislich mehr als 260 Zeichen besitzt. Zielpfad und gemessene Länge werden protokolliert; die Datei muss
   lesbar und zu der Datei im veröffentlichten Binaries-ZIP hashidentisch sein.
2. Die Desktopanwendung muss starten und genau eine synthetische CII-Beispielrechnung mit KoSIT erfolgreich
   prüfen. Die reguläre Deinstallation muss anschließend auch den langen Installationspfad entfernen.
3. Nach Rückkehr zum sauberen Snapshot wird ausschließlich der Produktions-Dienstinstaller im Standardpfad
   installiert. Es genügt der Nachweis `Running`, ein Healthcheck mit Zielversion, `status=ok` und
   betriebsbereitem KoSIT sowie das sichtbare Öffnen der Oberfläche über den Öffnen-Client; anschließend wird
   regulär deinstalliert.

Unter Windows 10 entfallen Upgrades, Alt-Tab-/Cachetests, die vollständige Dokumentformatmatrix,
Konfigurations-/Tokenerhalt, Modusausschluss, Rollback, Recovery, Reboot und Performanceprüfungen. Ein
reproduzierbarer produktbedingter Fehler bei Installation, langem XSD-Zielpfad, Start, KoSIT-Smoke oder
Dienst-Lauffähigkeit bleibt ein Releaseblocker. Nur ein nachweislich betriebssystem- oder umgebungsbedingter
Befund kann durch dokumentierte Einzelentscheidung als nicht blockierend eingestuft werden; aus der
Best-Effort-Kompatibilität entsteht keine vollständige Supportzusage.

Unter Windows 11 verbleiben zwei fokussierte Upgrades von der unmittelbar vorher veröffentlichten Patchversion
derselben Release-Linie. Für 2.0.2 sind das die unveränderten, veröffentlichten und signierten
2.0.1-Produktinstaller als Baseline auf zwei getrennten Snapshots:

1. Beim Desktop-Upgrade bleibt ein Alt-Tab mit warmem Cache geöffnet. Es muss kontrolliert mit
   `403 desktop_session_error` oder `409 ui_version_mismatch` samt Wiederöffnungshinweis enden. Ein neues
   Launcher-Fenster muss unmittelbar die aktuelle Oberfläche laden; ein JavaScript-Stackfehler wie
   `Cannot set properties of null` ist ein Releaseblocker. Genau eine sichtbare UBL-Beispielprüfung mit KoSIT
   genügt.
2. Beim Upgrade des laufenden Dienstes werden Konfigurationsdatei und API-Token vorher und nachher
   hashgebunden verglichen. Danach müssen Dienst und Healthcheck erfolgreich sein und der Öffnen-Client sichtbar
   die aktuelle Oberfläche laden.

Folgende Prüfungen sind **nur bei den jeweils genannten Auslösern** verpflichtend. Eine Zielversion `X.Y.0` löst
nur die ausdrücklich mit „neue Minor-Version“ markierten Punkte aus:

- Upgrade von der ältesten noch unterstützten Ausgangsversion bei Änderungen an Migration,
  Altzustandserkennung oder unterstützten Upgradepfaden sowie für eine neue Minor-Version; für 2.0.2 wäre die
  historische Ausgangsversion 1.5.0, der Lauf wird aber nicht ausgelöst;
- tatsächlicher Windows-Neustart und Dienststart vor Anmeldung bei Änderungen an SCM-, Dienststart- oder
  Dienstinstallerlogik sowie für eine neue Minor-Version;
- persistente `-CommitHardKillRecovery LeaveForReboot`-Abnahme bei Änderungen an Installertransaktionen,
  Markern oder Recovery beziehungsweise nach einem ungeklärten Immediate-Recovery-Befund;
- echte zweite lokale Testidentität und Offline-Hive bei Änderungen an Profilinventur oder Modusausschluss;
- Edge-/RDP-CPU-Vergleich bei Änderungen an Polling, Ladeanimation oder DOM-Aktualisierung während einer
  KoSIT-Ausführung sowie nach einem konkreten Performancebefund;
- vollständiger Node-RED-Mailflow vor Anmeldung nur, wenn dieser konkrete Betriebsfall Teil der Freigabe ist.

Für jeden Release wird zu diesen Auslösern lediglich `ja/nein` mit einer kurzen Begründung protokolliert. Ist
kein Auslöser erfüllt, entfallen die betreffenden Läufe vollständig.

Defender-/SmartScreen-Beobachtungen werden protokolliert, sind wegen externer Reputationsentscheidungen aber kein
deterministischer Releaseblocker. Negative Tokenfälle, Schema- und Berichtsheader, PDF/XML, KoSIT-Annahme und
-Ablehnung, Updates laufender und gestoppter Dienste, Fehler-Rollback, beide Deinstallationsvarianten,
Modusausschluss und Immediate-Recovery werden durch den Produktions-Desktop-, den Modusausschluss- und den
internen Dienst-Harness nachgewiesen und nicht zusätzlich manuell wiederholt.

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
der Lauf fehl. Vor der manuellen Veröffentlichung werden genau die Dateien des Drafts heruntergeladen,
vollständig gegen Inventar, Prüfsummen und Signaturen geprüft; das interne Recovery-Testartefakt darf im Draft
nicht enthalten sein. Der Desktopinstaller wird auf einem sauberen Windows-11-x64-Snapshot frisch installiert.
Bestehenskriterien sind eine sichtbare aktuelle Oberfläche, ein Healthcheck mit Zielversion und `status=ok`, ein
einfacher synthetischer Analyseaufruf sowie eine erfolgreiche Deinstallation ohne verbliebenen Programmbaum.
Ein Fehler sperrt die Veröffentlichung des Drafts. Vorab- und Tag-Build können wegen Build- und
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
