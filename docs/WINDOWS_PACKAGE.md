# Windows-x64-Pakete

## Betriebsarten und Artefakte

Das Endbenutzerpaket läuft auf Windows x64 ohne separat installiertes Python, Java oder KoSIT. Seit Version 1.4.0
stehen zwei bewusst getrennte Betriebsarten bereit:

| Betriebsart | Installer | Installation | Start |
|---|---|---|---|
| Desktop/Tray | `E-Rechnungs-Pruefer-<Version>-Windows-x64-Setup.exe` | benutzerbezogen, standardmäßig unter `%LOCALAPPDATA%\Programs\E-Rechnungs-Pruefer` | manuell oder optional nach Benutzeranmeldung |
| Windows-Dienst | `E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe` | systemweit unter `%ProgramFiles%\E-Rechnungs-Pruefer-Dienst` | manuell oder standardmäßig `Automatic (Delayed Start)` |

Der Desktop-Installer behält seine eigene App-ID und benötigt keine Administratorrechte. Der Dienst-Installer hat
eine andere App-ID, verlangt Administratorrechte und registriert niemals eine ausführbare Datei aus
`%LOCALAPPDATA%` als Dienst. Beide Installer enthalten dieselbe Anwendung sowie die festgeschriebenen
KoSIT-/XRechnung-Komponenten. Der Desktop-Installer unterstützt weiterhin einen benutzerdefinierten Zielordner
und behält ihn bei Updates bei. Windows ARM64 ist kein Ziel dieser Pakete.

Zusätzlich entsteht
`E-Rechnungs-Pruefer-<Version>-Windows-x64-Binaries.zip` sowie
`E-Rechnungs-Pruefer-<Version>-Windows-x64-SHA256SUMS.txt`. Das ZIP veröffentlicht die vollständigen signierten
Desktop- und Dienstbundles samt Öffnen-Client. Nach dessen Entpacken enthält die Prüfsummendatei direkt prüfbare
SHA-256-Werte der drei eigenen EXEs, beider Installer und zusätzlich des ZIP-Archivs.

## Gemeinsame Laufzeit

Desktop und Dienst verwenden denselben FastAPI-, Parser-, Prüf- und Berichtscode. Beide binden ausschließlich an
`127.0.0.1` und verwenden standardmäßig den festen Port `8080`; es gibt keinen automatischen Ausweichport. Ein
maschinenweiter Backend-Mutex verhindert zusammen mit der exklusiven Portreservierung, dass Desktopserver und
Dienst gleichzeitig laufen. Bereits ein Konflikt führt zu einem kontrollierten, geschlossenen Startfehler.

Der Serverlebenszyklus aktiviert die jeweilige Konfiguration vor dem Import von `app.main`, weil Settings und
Sicherheitsmiddleware beim Import ausgewertet werden. Es gibt keinen HTTP-Shutdown-Endpunkt. Uploads,
Prüfberichte und Original-XML werden auch in den installierten Betriebsarten nicht dauerhaft gespeichert.

## Desktop-/Tray-Modus

`app/windows_launcher.py` startet Uvicorn mit dem vorab reservierten Loopback-Socket und öffnet den Standardbrowser
erst nach erfolgreichem Healthcheck. Ein benutzerbezogener Windows-Mutex verhindert mehrere Tray-Instanzen. Ein
zweiter normaler Start öffnet die vorhandene Sitzung erneut. Das Symbol im Windows-Infobereich bietet **Öffnen**
und **Beenden** an.

Der Desktopmodus erzeugt pro Start ein zufälliges Browser-Token. Der einmalige Startlink setzt ein
`HttpOnly`-/`SameSite=Strict`-Cookie und leitet auf die tokenfreie Startseite um. Danach werden Host, Sitzung und
bei schreibenden Browseranfragen der Origin geprüft. Die geschützte Laufzeitdatei unter
`%LOCALAPPDATA%\E-Rechnungs-Pruefer` enthält Port, Prozess-ID und Browser-Token.

Für Automatisierungen erzeugt der Launcher ein davon getrenntes persistentes Bearer-Token unter
`%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt`. Es schützt die fachlichen `/api/*`-Endpunkte; `/api/health`
bleibt als lokaler Healthcheck tokenfrei. Das Token erscheint weder in der Browser-URL noch in `runtime.json`.
Der Parameter `--background` startet Server und Infobereich ohne automatisches Browserfenster.

Die optionale, standardmäßig abgewählte Installeraufgabe **Bei Windows-Anmeldung automatisch starten** legt für
den aktuellen Benutzer einen exakten Eintrag unter
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` mit `--background` an. Sie bleibt nicht privilegiert, ist
kein Windows-Dienst und beginnt erst nach der Anmeldung. Bei einem Desktop-Update wird eine laufende aktuelle
App über ihr lokales Shutdown-Ereignis beendet und nur bei zuvor laufendem, weiterhin ausgewähltem Autostart im
Hintergrund neu gestartet.

## Windows-Dienstmodus

Der administrative Installer registriert `ERechnungsPrueferService` mit dem Anzeigenamen
**E-Rechnungs-Prüfer Dienst**. Das Dienstprogramm liegt im unveränderlichen ProgramFiles-Bundle; ein kleiner
interaktiver Client `E-Rechnungs-Pruefer-Oeffnen.exe` liegt im selben atomar aktualisierten und rollbackfähigen
Bundle und wird im gemeinsamen Startmenü als
**E-Rechnungs-Prüfer öffnen** eingetragen.

Der Dienst läuft als `NT AUTHORITY\LocalService`, nicht als `LocalSystem`. Der uneingeschränkte dienstspezifische
SID `NT SERVICE\ERechnungsPrueferService` wird aktiviert. Standardmäßig konfiguriert der Installer
`Automatic (Delayed Start)` und zwei verzögerte Neustartversuche bei unerwartetem Ausfall. Wird die Aufgabe
**Beim Systemstart starten (verzögert)** abgewählt, erhält der Dienst den Starttyp `Manual`.

Maschinenzustand liegt ausschließlich unter `%ProgramData%\E-Rechnungs-Pruefer`:

- `service.json`: streng validierte Konfiguration mit Schema-Version, Port, KoSIT-Aktivierung und
  KoSIT-Zeitgrenze;
- `api-token.txt`: persistentes API-Bearer-Token;
- `logs\service.log`: rotierendes technisches Lebenszyklusprotokoll.

Die Bind-Adresse ist kein Konfigurationsfeld und bleibt fest `127.0.0.1`. Verzeichnis, Konfiguration, Token und
Log erhalten geschützte DACLs für `SYSTEM`, lokale Administratoren und den Service-SID. `Everyone`,
`Authenticated Users`, interaktive Sammelidentitäten und lokale oder domänenweite Gruppen erhalten keinen
pauschalen Tokenzugriff. Als zusätzlicher Tokenleser sind nur konkret auflösbare Benutzer-, Computer-/gMSA- oder
dienstspezifische `S-1-5-80-…`-Identitäten zulässig. Das technische Log
enthält keine Authorization-Header, Tokens, Rechnungsbytes oder fachlichen Rechnungsinhalte.
Der ProgramData-Stamm wird über die Windows-Known-Folder-API statt über eine veränderbare Umgebungsvariable
bestimmt. Verzeichnis, Konfiguration und Token erhalten `BUILTIN\Administrators` als Besitzer. Vor Lesen,
Schreiben oder ACL-Änderungen werden unbekannte Besitzer, Reparse-Points/Junctions und Datei-Hardlinks
geschlossen abgewiesen; konkrete zusätzliche Tokenleser werden nur nach erneuter positiver Prüfung erhalten.
Hat Windows Explorer nach einer bestätigten Zugriffsabfrage auf dem ProgramData-Stamm oder dem Logverzeichnis
einen expliziten Benutzer-Vollzugriffs-ACE ergänzt, blockiert dies weder den nächsten Systemstart noch eine
Neuinstallation. Akzeptiert wird nur genau ein direkter Benutzer der lokalen Administratorgruppe mit exakt
explizitem `Full Control` und `OI|CI`; auf Dateien, für Gruppen, mit abweichenden Rechten oder bei mehreren
Zusatzidentitäten bleibt die Prüfung geschlossen. Der Dienst entfernt diesen ACE beim nächsten Start wieder vom
Stamm und schützt die Logpfade vor dem Öffnen neu; die erhöhte Setup-Vorprüfung normalisiert ihn vor dem Lesen
der Maschinenkonfiguration.

### Sichere Browseröffnung

Ein Dienst in Session 0 öffnet weder Tray, Browser noch MessageBox. Der interaktive Öffnen-Client verbindet sich
stattdessen mit einer lokalen Named Pipe. Die Pipe lehnt Remoteclients ab, prüft die interaktive Windows-Sitzung,
und der Client verifiziert, dass der Pipe-Serverprozess zum beim SCM registrierten Dienst gehört. Das Protokoll
kennt ausschließlich den Befehl zum Öffnen der Oberfläche. Der Client bestätigt den Empfang der exakten
Antwortbytes, bevor der Dienst die Pipe leert und trennt; jede Phase unterliegt derselben kurzen Zeitgrenze. Der
Dienst hält die erste Pipe-Instanz während seiner gesamten Laufzeit offen; interaktive Clients erhalten nur die
für Lesen, Schreiben und Pipeattribute nötigen Rechte, ausdrücklich aber kein Recht zum Erzeugen einer
konkurrierenden Pipe-Instanz. Schlägt eine IPC-Phase technisch fehl, enthält das geschützte Dienstprotokoll nur
den festen Phasennamen, den Exception-Typ und gegebenenfalls den numerischen Windows-Fehler; Anfrage,
Browseradresse und Token werden nicht protokolliert.

Der Dienst liefert über die Pipe nur einen zufälligen HTTP-Bootstrap, der höchstens 60 Sekunden gültig und genau
einmal verwendbar ist. Der Bootstrap wird gegen ein zufälliges, zeitlich begrenztes
`HttpOnly`-/`SameSite=Strict`-Cookie getauscht und danach aus der sichtbaren URL entfernt. Das dauerhafte
API-Bearer-Token gelangt weder über die Pipe noch in URL, Cookie, Browser-Speicher oder normale Logs. Der Dienst
hält höchstens 32 ausstehende Bootstraplinks und 128 aktive Browsersitzungen; bei voller Kapazität wird jeweils
der älteste Eintrag verdrängt. Damit bleiben Speicher- und Bereinigungsaufwand auch bei missbräuchlichen lokalen
Anfragen hart begrenzt.

### SCM-Start und -Stopp

Der Dienst meldet `START_PENDING`, `RUNNING`, `STOP_PENDING` und `STOPPED` an den Service Control Manager. Beim
Stoppen werden IPC und Server geordnet beendet. Aktive KoSIT-Unterprozesse erhalten eine begrenzte
Beendigungsphase und werden nötigenfalls beendet; die gesamte SCM-Wartegrenze beträgt die konfigurierte
KoSIT-Zeitgrenze plus 15 Sekunden. Die Dienstkonfiguration begrenzt die KoSIT-Zeitgrenze auf höchstens 300
Sekunden. Vor dem ersten Java-Start ordnet sich der Dienst einem Windows-Job-Objekt mit
`KILL_ON_JOB_CLOSE` zu; dadurch gehört bereits die Prozesserzeugung zum Job und ein harter Dienstabbruch beendet
auch den vollständigen Java-Prozessbaum. stdout, stderr und der XML-Prüfbericht besitzen feste Bytebudgets.

Die Dienst-EXE ist kein interaktiver Anwendungsstarter. Wird sie aus einer angemeldeten Windows-Sitzung direkt
ausgeführt, endet sie kontrolliert und verweist auf `E-Rechnungs-Pruefer-Oeffnen.exe`. Beim SCM-Start in Session 0
wird keine Meldung angezeigt.
Die materialisierte Rechnungs-XML wird exklusiv neu angelegt, bleibt für den Java-Prozess lesbar und wird nach
jeder regulären, fehlgeschlagenen oder abgebrochenen Prüfung im `finally`-Pfad entfernt. Windows-Delete-on-close
wird bewusst nicht verwendet, weil dessen Delete-Sharing den normalen Datei-Open des Java-Prozesses blockiert.
Im Dienstmodus liegt der zufällige KoSIT-Tempbaum unter dem zuvor erneut verifizierten, privaten
`%ProgramData%\E-Rechnungs-Pruefer\runtime`-Elternpfad und wird dort atomar mit einer geschützten, vererbbaren DACL
für Service-SID, `SYSTEM` und Administratoren angelegt. Dadurch kann ein anderer Prozess unter dem gemeinsam
genutzten `LocalService`-Konto den Baum weder lesen noch über den Elternpfad umbenennen oder austauschen; ein
`OWNER RIGHTS`-ACE begrenzt zusätzlich die impliziten Besitzerrechte. XML und VARL-Berichte erben diese DACL
bereits bei ihrer Erstellung. Nach einem unkontrollierten Betriebssystem- oder Prozessabbruch kann dieser
geschützte Tempbaum kurzzeitig zurückbleiben. Der nächste Dienststart inventarisiert Owner, DACL, Objekttypen,
Hardlinks und Reparse-Points vollständig und entfernt ausschließlich exakt passende verwaiste KoSIT-Läufe.
Ein technischer Abbruch bleibt ein technischer Fehler und wird nie als fachliche KoSIT-Ablehnung ausgegeben.
Stirbt der Web- oder IPC-Thread unerwartet, beendet sich der Dienst mit technischem Fehler, damit die
konfigurierten SCM-Recovery-Aktionen tatsächlich greifen.

## API-Token für Node-RED provisionieren und rotieren

Der Automatisierungsvertrag verwendet `Authorization: Bearer <Token>` gegen
`http://127.0.0.1:8080/api/report/pdf` und fordert `scope=readable` an. Das Diensttoken darf nicht durch
Leserechte für allgemeine lokale Gruppen freigegeben werden. Zuerst muss die konkrete Windows-Identität des
Node-RED-Prozesses ermittelt werden. Anschließend kann ein Administrator genau dieser Identität Leserechte geben:

```powershell
$DienstExe = "$env:ProgramFiles\E-Rechnungs-Pruefer-Dienst\service\E-Rechnungs-Pruefer-Dienst.exe"
& $DienstExe --grant-token-read "DOMAENE\svc-node-red"
```

Der Tokenwert wird danach unter dieser Identität kontrolliert in den Node-RED-Credential-Speicher oder dessen
geschützte Prozessumgebung übernommen. Er gehört nicht in den exportierten Flow.

Eine Rotation ist nur bei gestopptem Dienst zulässig:

```powershell
Stop-Service ERechnungsPrueferService
& $DienstExe --rotate-token
Start-Service ERechnungsPrueferService
```

Die Rotation übernimmt ausschließlich zuvor verifizierte, konkrete Leser-SIDs; breite oder unbekannte
Schreibberechtigungen führen zum geschlossenen Abbruch. Danach müssen die geschützte Node-RED-Konfiguration mit
dem neuen Token aktualisiert und der Node-RED-Prozess neu gestartet werden.
Weitere Hinweise zur Identität und zum Betrieb vor Anmeldung stehen in [`NODE_RED.md`](NODE_RED.md).

## Installation, Wechsel der Betriebsart, Update und Deinstallation

Nach dem UAC-Wechsel wartet der Dienst-Installer, bis sein Assistent tatsächlich sichtbar ist, und versucht
einmalig, ihn zu aktivieren. Windows darf diese Fokusübernahme ablehnen. In diesem Fall wird das Setup ohne
synthetische Eingaben für höchstens zehn Sekunden sichtbar über dem bisherigen Vordergrundfenster gehalten. Der
Hinweis endet sofort bei echter Aktivierung und wird weder auf späteren Seiten wiederholt noch dauerhaft als
Always-on-top beibehalten. Wird das Setup bereits auf der Lizenzseite abgebrochen und der Abbruch bestätigt,
endet es ohne eine Rollback- oder Bereinigungsroutine aufzurufen, die den noch nicht initialisierten
Installationspfad benötigt.

Dienst-Setup und -Deinstaller erwerben vor ihrem ersten Recovery- oder Änderungsschritt atomar denselben
systemweiten Named Mutex. Sie halten ihn über die vollständige mutierende Laufzeit einschließlich Commit,
Rollback und Cleanup. Dadurch können auch aus verschiedenen interaktiven Windows-Sitzungen gestartete Installations-,
Update- und Deinstallationsläufe nicht gleichzeitig auf SCM-, Bundle- oder Maschinenzustand zugreifen.
Ein belegter oder nicht sicher prüfbarer Mutex bricht den neuen Lauf geschlossen ab. Ein nach einem Prozessabbruch
übernommener Mutex führt weiterhin zuerst durch die persistente Recovery. Diese Vorgangssperre ist vom
Backend-Mutex getrennt, der den gleichzeitigen Anwendungsbetrieb verhindert.

Desktop und Dienst werden nicht automatisch ineinander überführt. Vor der Installation des Dienstmodus muss der
Desktopmodus über seinen registrierten Uninstaller vollständig entfernt werden; dies umfasst den
produktspezifischen HKCU-Autostart und den benutzerbezogenen Desktopzustand. Der Dienst-Installer beendet,
quarantänisiert oder verändert keine Desktopinstallation. Umgekehrt verweigert der Desktop-Installer die
Installation, solange der eigene Dienst registriert ist. Ein abgewiesener Installer darf weder die aktive
Betriebsart noch deren API-Token, Autostart, SCM-Zustand oder ProgramData verändern.

Der erhöhte Dienst-Preflight inventarisiert den Desktop-Gegenmodus read-only über alle in der Windows-
`ProfileList` registrierten lokalen und Entra-ID-Profile. Er prüft jeweils den Standardinstallationsordner, den
produktspezifischen Uninstall-Key und den HKCU-Autostart. Geladene Hives liest er über `HKEY_USERS`. Für ein
abgemeldetes Profil muss genau einer der beiden zulässigen Hives `NTUSER.DAT` oder `NTUSER.MAN` vorhanden und
no-follow prüfbar sein. Er wird mit einem gegen Schreiben und Löschen gesperrten Lesehandle größenbegrenzt in
einen einmaligen Speicher-Snapshot eingelesen und dort mit der exakt gepinnten Komponente Regipy ausgewertet.
Der Parser läuft in einem eigenen Hilfsprozess, den der erhöhte Preflight je Hive nach spätestens 30 Sekunden
beendet; die gesamte Offline-Inventur ist auf 60 Sekunden begrenzt. Timeout, Prozessfehler oder
Speichererschöpfung blockieren die Installation geschlossen. Der Scanner mountet den Hive nicht, verwendet
insbesondere kein `RegLoadAppKeyW` und legt keine temporäre Hive-Kopie an.

Eine unvollständige Profilinventur, ungültige oder nicht feste Profilpfade, Reparse-Points, Junctions, Hardlinks,
mehrdeutige Hives, ein Identitätswechsel während des Lesens, inkonsistente REGF-Prüfsummen, Sequenznummern oder
HBin-Ketten sowie ein ungültiger Root-Key-Verweis oder unvollständig auswertbare relevante Schlüssel und Werte
führen zum geschlossenen Abbruch. Die Registryprüfung erkennt dadurch auch vorhandene Desktopversionen in
benutzerdefinierten Zielordnern abgemeldeter Profile. Diese Prüfung entfernt oder repariert keinen Gegenmodus.

Das Diensttoken ist ein eigenständiges Maschinentoken und wird nicht aus dem Desktopprofil übernommen. Die
frühere Inno-Option `/MIGRATEDESKTOPTOKEN=1` wird ersatzlos nicht mehr unterstützt. Nach einem Wechsel müssen
Node-RED und andere Automatisierungen kontrolliert mit dem Token der neu installierten Betriebsart provisioniert
werden. Für die Rückkehr zum Desktopmodus wird zuerst der Dienst deinstalliert und anschließend der
Desktopmodus neu installiert.

Unvollständige v1.4.0-Migrations-, Transfer-, Seal-, Quarantäne- oder daran gebundene Alttransaktionszustände
werden von neueren Installern nicht übernommen und nicht automatisch wiederhergestellt. Eine betroffene
Produktivmaschine muss anhand einer gesicherten Diagnose in einen dokumentiert sauberen Ausgangszustand gebracht
werden; eine neue Installation darf nicht durch manuelles Löschen einzelner Transaktionsmarker erzwungen werden.
Automatisierte Paket- und Freigabetests beginnen deshalb immer auf einer sauberen Wegwerf-VM ohne v1.4.0-Altzustand.

Bei einem Dienstupdate wird der Dienst über SCM vor dem Ersetzen von Dateien deaktiviert und gestoppt; das Setup
wartet auf `STOPPED`. Konfiguration und Token bleiben erhalten. Nur ein vor dem Update laufender Dienst wird nach
erfolgreicher Installation wieder gestartet, und sein vorheriger Starttyp wird berücksichtigt. Setup- und
SCM-Zustand werden bei Fehlern zurückgerollt, statt eine halb aktualisierte Installation weiterzubetreiben. Das
Fehler-Rollback entfernt einen vollständig neu angelegten Maschinenzustand nur über denselben strikt
inventarisierenden Purge-Helfer und erst nach erneut bestätigter Abwesenheit des neuen SCM-Dienstes. Auch eine
explizite Deinstallationsbereinigung verlangt vor jedem ProgramData-Zugriff einen gestoppten oder entfernten Dienst.
Das vollständige Onedir-Bundle wird zunächst nach `service.new` entpackt und dann per Verzeichnisumbenennung aktiviert;
der alte Baum bleibt bis zum Commit als `service.rollback` erhalten. Dadurch verschwinden auch Dateien, die in der
neuen Version nicht mehr enthalten sind. Starttyp, verzögerter Start, Beschreibung, Service-SID und Recovery werden
ausschließlich über SCM-Abfrage- und Änderungs-APIs gesichert und exakt restauriert.

Vor der ersten SCM- oder Maschinenmutation schreibt der Öffnen-Client ein unveränderliches
`PREPARED`-Manifest unter
`%ProgramFiles%\E-Rechnungs-Pruefer-Dienst\.installer-state`. Es bindet Transaktions-ID, ursprüngliche
SCM-Metadaten, Maschinenzustand und Zielzustand. Erst nachdem neuer Bundlebaum, stabiler Dienst,
Maschinenzustand und Healthcheck bewiesen sind, wird dort atomar `COMMIT_STARTED` veröffentlicht. Ein späterer
Setupstart reconciliert einen unterstützten service-only Beleg vor dem normalen Preflight: vor
`COMMIT_STARTED` ausschließlich zurück zur exakten Baseline, danach ausschließlich vorwärts zum bereits
bewiesenen Ziel. Fehlende, fremde, widersprüchliche oder nicht eindeutig zuordenbare Belege beziehungsweise
Bundle-/SCM-Zustände blockieren jede Recovery geschlossen. Die Belege werden erst nach der Servicebereinigung
entfernt.

Bei der Deinstallation wird der Dienst zuerst gestoppt, aus dem SCM gelöscht und sein vollständiges Verschwinden
abgewartet; diese Mutation beginnt erst nach bestätigter Deinstallation und nur, wenn ImagePath und Dienstkonto
weiter eindeutig zum Produkt gehören. Vor der ersten SCM-Änderung wird die vollständige Baseline einschließlich
des ursprünglichen RUNNING-Zustands atomar unter
`%ProgramFiles%\E-Rechnungs-Pruefer-Dienst\.uninstaller-state` veröffentlicht. Ein Folgelauf restauriert einen
noch vorhandenen Dienst samt Startzustand vollständig oder erkennt eine bereits abgeschlossene SCM-Löschung als
Vorwärtsfortschritt; erst danach wird der Beleg entfernt. Ein offener Deinstallationsbeleg blockiert Installation
und Update auch an der letzten Grenze vor dem Installationsmanifest. Danach werden die Binärdateien entfernt.
`%ProgramData%\E-Rechnungs-Pruefer` bleibt standardmäßig erhalten. Eine klar bezeichnete Benutzerentscheidung kann
die bekannten Konfigurations-, Log- und Tokendateien löschen. Vor dieser Löschung inventarisiert der noch installierte
Öffnen-Client den Known-Folder-Pfad erneut und akzeptiert nur die exakt bekannten Dateien und Logrotationen mit
vertrauenswürdigem Besitzer und enger DACL; unbekannte Einträge, Reparse-Points/Junctions, Hardlinks oder verbreiterte
Rechte brechen die Deinstallation geschlossen ab. Es gibt keine rekursive Löschung unbekannter operatorseitiger
Dateien. Der ausschließlich transiente `runtime`-Baum wird unabhängig von dieser Benutzerentscheidung nach derselben
vollständigen Inventur entfernt, damit Crashreste mit Rechnungsdaten nie über eine Deinstallation hinweg aufbewahrt
werden. Für unbeaufsichtigte Tests entspricht `/PURGEDATA=1` der ausdrücklichen Löschentscheidung für den übrigen
Maschinenzustand.

## Gesperrte Prüfkomponenten

`packaging/windows/components.lock.json` legt Downloadquelle und SHA-256-Prüfsumme fest für:

- Eclipse Temurin JRE für Windows x64;
- das ausführbare KoSIT-Standalone-JAR;
- die XRechnung-Validator-Konfiguration.

`scripts/prepare_windows_components.py` lädt diese Dateien in einen lokalen Cache, prüft jeden Hash und bereitet
`runtime/java/` und `vendor/kosit/` für PyInstaller vor. ZIP-Ziele werden vor dem Entpacken gegen Pfadtraversierung
geprüft. Bei einer Aktualisierung müssen Version, Dateiname, URL und veröffentlichte Prüfsumme gemeinsam geprüft
werden. Anschließend sind mindestens eine Annahme und eine Ablehnung real mit KoSIT zu testen.

## Lokaler Build auf Windows

Voraussetzungen sind Windows-x64-Python 3.13 und Netzwerkzugriff beim Vorbereiten der gesperrten Komponenten und
des auf Inno Setup 7.0.2 x64 festgeschriebenen Installercompilers:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . -r packaging\windows\requirements-build.txt
python scripts\prepare_windows_components.py
$InnoSetupCompiler = .\scripts\install_inno_setup.ps1
.\scripts\build_windows.ps1 -InnoSetupCompiler $InnoSetupCompiler
```

Für signierte GitHub-Builds gilt stattdessen der vollständige, gehashte Windows-x64-Lock
`packaging\windows\requirements-release.txt` zusammen mit CPython 3.13.14. Der Workflow installiert diesen Lock
mit `--require-hashes --only-binary=:all:` und anschließend das lokale Projekt ohne erneute
Abhängigkeitsauflösung. Änderungen am Lock sind eigenständige Releaseänderungen und müssen durch den
Windows-Pakettest geprüft werden.

Der Build erzeugt getrennte PyInstaller-Bundles für Desktop und Dienst sowie den kleinen Öffnen-Client. Ein
bewusst reduzierter Build mit `-WithoutOfficialValidation` ist nur für die Entwicklung bestimmt und darf nicht
veröffentlicht werden. PyInstaller ist kein Cross-Compiler; macOS eignet sich zur Entwicklung, aber nicht zum
Erzeugen oder Ausführen der Windows-Pakete.

## Signierung

`scripts/build_windows.ps1` signiert ausschließlich die drei anwendungseigenen EXEs
`E-Rechnungs-Pruefer.exe`, `E-Rechnungs-Pruefer-Dienst.exe` und `E-Rechnungs-Pruefer-Oeffnen.exe` sowie beide
Installer. Jede Signatur erhält einen RFC-3161-Zeitstempel und wird unmittelbar verifiziert. Erst danach wird die
vollständige Bundle-ZIP erstellt und die gemeinsame SHA-256-Datei geschrieben. Bereits signierte Drittkomponenten wie die eingebettete Java-Laufzeit
werden nicht mit einer Projektsignatur überschrieben.

Lokale Builds können `EINVOICE_SIGN_CERT_SHA1` für ein RSA-Code-Signing-Zertifikat im persönlichen
Zertifikatsspeicher verwenden. Der Release-Workflow nutzt AzureSignTool und den nicht exportierbaren HSM-Schlüssel
in Azure Key Vault über GitHub OIDC. PFX-Dateien und dauerhafte Azure-Client-Secrets werden nicht in GitHub
gespeichert.

## Automatisierte Paket-, Modusausschluss- und Recoverytests

Alle folgenden Skripte verändern reale Installer-, Dienst-, Registry- und Tokenzustände. Sie dürfen
ausschließlich in einer sauberen, entbehrlichen Windows-VM beziehungsweise unter einer eigenen Testidentität
laufen. `-ConfirmIsolatedEnvironment` bestätigt diese Voraussetzung, hebt die Vorabprüfungen aber nicht auf.
Insbesondere dürfen keine unvollständigen v1.4.0-Migrations- oder Alttransaktionszustände vorhanden sein.

```powershell
$InnoSetupCompiler = .\scripts\install_inno_setup.ps1
.\scripts\build_windows.ps1 -InnoSetupCompiler $InnoSetupCompiler -BuildElevatedRecoveryTestInstaller
.\scripts\test_windows_package.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_mode_exclusion.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_service_package.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedRecoveryTestContext
```

`-BuildElevatedRecoveryTestInstaller` erzeugt zusätzlich unter `build\windows\test-installer` einen ausschließlich
für den erhöhten, unbeaufsichtigten VM-Recoverytest bestimmten Dienst-Installer. Nur dieser Build enthält die
interne Freigabe für `/ALLOWELEVATEDTESTCONTEXT=1`; der produktive Installer unter `dist` enthält und akzeptiert
diesen Testpfad nicht. Der zusätzliche Test-Installer wird weder in das Prüfsummenmanifest noch in
Release-Artefakte aufgenommen. Beim signierten Vorab-Probelauf wird jedem Testskript zusätzlich
`-RequireSignature` übergeben.

Der Desktoptest deckt Installation, Browser-/API-Authentifizierung, PDF, bytegetreuen XML-Export, KoSIT,
HKCU-Autostart, laufendes Update und Deinstallation ab. Der Diensttest prüft unter anderem Dienstkonto, ImagePath,
Starttyp, konfigurierte und durch erzwungenen Prozessabbruch ausgelöste Recovery, SCM-Zustände, reine
Loopback-Bindung, geschützte DACLs samt effektiven Rechten, Browser-IPC, den Global-Mutex, API-Tokenfälle,
Tokenpersistenz über Stop/Start und Update, einen absichtlich fehlgeschlagenen Update-Rollback sowie den
vollständigen Bundlebaum, die Entfernung veralteter Dateien, den manuellen Starttyp, einen frühen Portkonflikt und
Deinstallation mit Erhalt und ausdrücklicher Löschung von ProgramData. Mit `-RequireSignature` werden zusätzlich
die installierten eigenen EXEs und Installer geprüft. Eine konkrete Windows-Testidentität wird als zusätzlicher
Tokenleser provisioniert; der Test rotiert das Token bei gestopptem Dienst und weist nach, dass genau ihr
schreibfreier ACE über Rotation, Update und Neuinstallation erhalten bleibt.

Der Modusausschlusstest installiert den aktuellen Desktopmodus mit Autostart, startet ihn bis zur Tokenanlage und
weist nach, dass der Dienst-Installer mit einem Fehlercode abbricht, ohne Desktopprozess, Dateien, Token oder
Autostart zu verändern. Zusätzlich prüft er einen abgemeldeten Benutzerhive mit benutzerdefiniertem
v1.3-Installationspfad und getrennt mit reinem Autostart-Footprint. Der Hive darf dabei weder geladen noch
byteinhaltlich verändert werden. Nach der regulären Desktopdeinstallation und Bereinigung der Testfootprints
muss derselbe saubere Offline-Hive eine Dienstinstallation zulassen. Bei
registriertem, für eine stabile Inhaltsprüfung gestopptem Dienst muss der Desktop-Installer seinerseits mit einem
Fehlercode abbrechen und Dienstbundle, SCM-Metadaten sowie ProgramData byteinhaltlich unverändert lassen. Der Test
übernimmt keine Tokens zwischen den Betriebsarten. Anschließend deinstalliert er den Dienst unter Erhalt von
ProgramData, installiert und entfernt den Desktopmodus ohne Änderung dieses Maschinenzustands und weist bei der
Dienstneuinstallation die Wiederverwendung desselben Maschinentokens nach.

Auf einer Wegwerf-VM kann zusätzlich der echte service-only Prozessabbruch-Checkpoint gefahren werden:

```powershell
.\scripts\test_windows_service_package.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedRecoveryTestContext `
    -CommitHardKillRecovery Immediate
```

Der Helfer unterbricht ein Update erst nach einem hashgebundenen `COMMIT_STARTED`-Marker und startet denselben
Installer erneut; dieser muss den bereits committed Dienst ausschließlich vorwärts bereinigen. Der Test besteht
nur, wenn der vollständig geparste, DACL-geprüfte Marker nach dem harten Abbruch noch unverändert vorhanden ist.
Ist das Checkpointfenster nicht eindeutig erreicht oder Setup bereits beendet, bricht der Test ab und meldet den
Checkpoint ausdrücklich nicht als ausgeführt.

CI verwendet frische Windows-Runner für diese zerstörenden Paketprüfungen. Sie ersetzen keine manuelle
Endabnahme.

### Reboot-Abnahme der persistenten Recovery

Diese zweistufige Prüfung ist nicht bei jedem Release auszuführen. Sie wird nur durch die in
[`RELEASE.md`](RELEASE.md#risikobasierte-manuelle-windows-abnahme) genannten Recovery- oder
Installertransaktionsänderungen beziehungsweise einen ungeklärten Immediate-Recovery-Befund ausgelöst.

Ein realer Stromverlust oder Hypervisor-Reset wird bewusst nicht aus einem Testskript ausgelöst. Für die
zweistufige VM-Abnahme hält der Hard-Kill-Helfer mit `LeaveForReboot` den exakt verifizierten persistenten
Zustand fest und beendet sich anschließend absichtlich mit Exitcode `194`, also nicht als bestandener Gesamttest:

```powershell
.\scripts\test_windows_service_package.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedRecoveryTestContext `
    -CommitHardKillRecovery LeaveForReboot
```

Nach dem Lauf:

1. Vor dem Neustart anhand der Skriptausgabe und Exitcode `194` bestätigen, dass der gewünschte Marker nach dem
   harten Setupabbruch erhalten blieb. Ein anderer Abbruch ist kein durchgeführter Checkpoint.
2. Die VM tatsächlich hart zurücksetzen oder ausschalten und erneut starten; keinen Snapshot auf den Zustand vor
   dem Checkpoint zurücksetzen.
3. Nach Anmeldung exakt denselben Testinstaller erneut mit
   `"/VERYSILENT"`, `"/SUPPRESSMSGBOXES"`, `"/NORESTART"`,
   `'/TASKS="systemstart"'` und `"/ALLOWELEVATEDTESTCONTEXT=1"` starten.
4. Den laufenden eigenen Dienst, das unveränderte Token und die Abwesenheit der
   `commit-recovery-sentinel.txt` nachweisen.
5. Die Abwesenheit von `service.new`, `service.rollback`, `service.obsolete` und
   `%ProgramFiles%\E-Rechnungs-Pruefer-Dienst\.installer-state` prüfen. Ein verbliebener oder widersprüchlicher
   Zustand zählt nicht als erfolgreiche Recovery und darf nicht manuell gelöscht werden, bevor
   Diagnoseinformationen gesichert sind.

## Risikobasierte manuelle Windows-Abnahme vor Veröffentlichung

Die vollständige verbindliche Matrix und ihre Releaseblocker stehen in
[`RELEASE.md`](RELEASE.md#risikobasierte-manuelle-windows-abnahme). Die Paket-Harnesses auf Windows Server 2022
decken mit dem Produktions-Desktopinstaller, dem Produktions-Dienstinstaller im Modusausschlusstest und dem
getrennten internen Recovery-Testinstaller die technischen API-, Update-, Rollback-, Preserve/Purge- und
Immediate-Recoveryfälle ab. Diese Prüfungen werden nicht auf jedem Clientsystem manuell dupliziert.

Windows 10 22H2 x64 im Home/Pro-Kanal wird nach Ende des regulären Microsoft-Supports nur als
Best-Effort-Kompatibilität geprüft:

1. signierte Desktop-Neuinstallation in einen Zielpfad, bei dem eine enthaltene KoSIT-XSD nachweislich mehr als
   260 Zeichen erreicht, anschließend Start, eine CII-KoSIT-Prüfung und reguläre Deinstallation;
2. nach Snapshot-Rückkehr signierte Dienst-Neuinstallation im Standardpfad, `Running`, Healthcheck,
   sichtbarer Öffnen-Client und reguläre Deinstallation.

Upgrades, Alt-Tabs, Formatmatrizen, Konfigurations-/Tokenerhalt, Modusausschluss, Recovery und Reboot werden auf
Windows 10 nicht wiederholt. LTSC-Ausgaben sind durch den 22H2-Kompatibilitätslauf nicht abgedeckt.

Auf Windows 11 x64 verbleiben nur das Desktop- und Dienst-Upgrade von der unmittelbar vorher veröffentlichten
Patchversion derselben Release-Linie; für 2.0.2 dienen die signierten 2.0.1-Produktinstaller als Baseline. Der
Desktopfall prüft ein warmes Alt-Tab, den kontrollierten `403 desktop_session_error`/`409 ui_version_mismatch`,
die aktuelle Oberfläche in einem neuen Fenster und einen sichtbaren UBL-KoSIT-Lauf. Der Dienstfall vergleicht
Konfiguration und API-Token hashgebunden und prüft danach Dienst, Healthcheck und Öffnen-Client.

Upgrade von der ältesten unterstützten Ausgangsversion und echter Neustart vor Anmeldung sind bei einschlägigen
Änderungen sowie für eine neue Minor-Version erforderlich. Persistente `LeaveForReboot`-Recovery, zweite
Testidentität, RDP-/Edge-Performance und ein realer Node-RED-Mailflow werden ausschließlich durch die in
`RELEASE.md` genannten fachlichen Änderungen oder Befunde ausgelöst. Defender-/SmartScreen-Beobachtungen sind
informativ und kein deterministischer Releaseblocker.

Je Release genügt für diese Auslöser eine protokollierte `ja/nein`-Entscheidung mit kurzer Begründung. Ist kein
Auslöser erfüllt, werden diese Prüfungen nicht ausgeführt.

## Drittkomponenten

Die mitgelieferten Lizenz- und NOTICE-Dateien der offiziellen Archive bleiben im Bundle erhalten, soweit sie
Bestandteil der Archive sind. Ergänzende Angaben stehen in `THIRD_PARTY.md`. Vor kommerzieller Verwendung sind
insbesondere die aktuellen Bedingungen von Inno Setup und die Weitergabebedingungen aller gebündelten
Komponenten zu prüfen.
