# Windows-x64-Pakete

## Betriebsarten und Artefakte

Das Endbenutzerpaket läuft auf Windows x64 ohne separat installiertes Python, Java oder KoSIT. Version 1.4.0
stellt zwei bewusst getrennte Betriebsarten bereit:

| Betriebsart | Installer | Installation | Start |
|---|---|---|---|
| Desktop/Tray | `E-Rechnungs-Pruefer-<Version>-Windows-x64-Setup.exe` | benutzerbezogen unter `%LOCALAPPDATA%\Programs\E-Rechnungs-Pruefer` | manuell oder optional nach Benutzeranmeldung |
| Windows-Dienst | `E-Rechnungs-Pruefer-<Version>-Windows-x64-Dienst-Setup.exe` | systemweit unter `%ProgramFiles%\E-Rechnungs-Pruefer-Dienst` | manuell oder standardmäßig `Automatic (Delayed Start)` |

Der Desktop-Installer behält seine eigene App-ID und benötigt keine Administratorrechte. Der Dienst-Installer hat
eine andere App-ID, verlangt Administratorrechte und registriert niemals eine ausführbare Datei aus
`%LOCALAPPDATA%` als Dienst. Beide Installer enthalten dieselbe Anwendung sowie die festgeschriebenen
KoSIT-/XRechnung-Komponenten. Windows ARM64 ist kein Ziel dieser Pakete.

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
`%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt`. Es schützt ausschließlich `/api/*` und erscheint weder in der
Browser-URL noch in `runtime.json`. Der Parameter `--background` startet Server und Infobereich ohne automatisches
Browserfenster.

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

Der unveränderte Automatisierungsvertrag verwendet
`Authorization: Bearer <Token>` gegen `http://127.0.0.1:8080/api/report/pdf`. Das Diensttoken darf nicht durch
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

## Installation, Moduswechsel, Update und Deinstallation

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
Update- und Deinstallationsläufe nicht gleichzeitig auf SCM-, Bundle-, Migrations- oder Maschinenzustand zugreifen.
Ein belegter oder nicht sicher prüfbarer Mutex bricht den neuen Lauf geschlossen ab. Ein nach einem Prozessabbruch
übernommener Mutex führt weiterhin zuerst durch die persistente Recovery. Diese Vorgangssperre ist vom
Backend-Mutex getrennt, der den gleichzeitigen Anwendungsbetrieb verhindert.

Der Dienst-Installer prüft vor Änderungen vorhandene Desktop- und Dienstinstallationen, Autostarts,
laufende Backends in allen Sitzungen, den festen Port, ProgramData und Tokens. Die registrierten und die bekannten
standardmäßigen Desktoppfade aller lokalen Benutzerprofile einschließlich Entra-ID-Profilen werden inventarisiert.
Dazu werden auch nicht geladene Benutzerhives unter einem Komponenten-Lock in eine administrative, temporäre
Momentaufnahme kopiert, nur diese Kopie unter einem zufälligen installationsspezifischen Namen eingehängt und
wieder ausgehängt; `NTUSER.DAT` und verpflichtende `NTUSER.MAN`-Hives werden berücksichtigt, ein
nicht eindeutig prüfbares Profil bricht den Wechsel ab. Profil-, Registrierungs- und Quarantänepfade müssen auf
einem festen lokalen Laufwerk liegen. Alle vorhandenen Komponenten werden no-follow geöffnet und ohne Schreib-
oder Löschfreigabe bis zum Abschluss der jeweiligen Prüfung gehalten; UNC-/Gerätepfade, Reparse-Points/Junctions und
Hardlinks werden vor einem tieferen Zugriff abgewiesen. Ein verbliebener produktspezifischer `Run`-Wert oder eine weitere
v1.3-Installation bricht den Moduswechsel geschlossen ab und muss zunächst entfernt werden. Der im ursprünglichen
interaktiven Benutzerkontext erzeugte Migrationsbeleg wird nach der erhöhten Inventur in einem festen,
administratorgeschützten Installer-Zustand versiegelt. Der Originalbenutzer besitzt dort ausschließlich
Leserechte, aber keine Lösch-, Schreib- oder Erstellrechte. Nach Commit oder Rollback entfernt nur der erhöhte,
signierte Verwaltungsclient den Beleg. Commit und Rollback verwenden ausschließlich diese Momentaufnahme und
nicht erneut den veränderbaren Tempbeleg oder HKCU-Pfad; so gibt eine UAC-Anmeldung mit abweichendem
Administratorkonto nicht versehentlich dessen HKCU-Pfad frei. Ein Desktop-Backend und der Dienst dürfen nicht
parallel betrieben werden. Maschinenzustand und Port werden durch den vorab aus dem signierten Setup extrahierten
Öffnen-Client geprüft, bevor Produktdateien ersetzt werden. Das Setup muss normal aus der interaktiven
Benutzeridentität gestartet werden; ein bereits erhöht gestarteter Migrationskontext wird geschlossen abgewiesen.

Plan und optionale Tokenübergabe werden nicht in Inno Setups privatem `{tmp}` erzeugt. Das erhöhte Setup stellt
den Hilfsclient stattdessen in einem eindeutigen, kurzlebigen Transferbaum unter
`%ProgramData%\E-Rechnungs-Pruefer-Installer-Transfer\<Setup-ID>` bereit. Geschützte DACLs geben `SYSTEM` und
lokalen Administratoren Vollzugriff; `INTERACTIVE` erhält am Baum nur Durchquerung, am Blatt zusätzlich das
Anlegen neuer Dateien und am Client Lesen/Ausführen. Vorhandene Transferdateien können dadurch nicht geändert
oder gelöscht werden, und Tokeninhalte sind nicht allgemein lesbar. Beleg und Token besitzen eigene exakte DACLs
für die konkrete Originalbenutzer-SID sowie `SYSTEM` und lokale Administratoren. Vor der Übernahme prüft der
erhöhte Client exaktes Inventar, Besitzer, alle Pfadkomponenten no-follow und Hardlinkfreiheit. Danach entfernt
das Setup nur die bekannten Dateien und leeren Verzeichnisse nichtrekursiv; unbekannte Einträge lassen die
Bereinigung geschlossen fehlschlagen.

Beim Wechsel **Desktop → Dienst** beendet ein Hilfsprozess in der ursprünglichen interaktiven Benutzeridentität
die Tray-App kontrolliert und entfernt nur den erwarteten HKCU-Autostartwert. Die Übernahme eines vorhandenen,
gültigen Desktop-API-Tokens ist eine eigene, standardmäßig nicht ausgewählte Option. Nur nach ausdrücklicher
Zustimmung wird es kopiert und vor der Veröffentlichung mit der Maschinen-DACL neu geschützt. Bei einem
Installationsfehler stellt das Setup den zuvor erfassten Autostartzustand wieder her. Die alte Desktopinstallation
wird während der Transaktion durch Umbenennen ihrer Backend-EXE portunabhängig deaktiviert. Bei einem Fehler wird
die EXE atomar zurückbenannt und ein zuvor laufender Desktop neu gestartet; nach erfolgreichem Wechsel wird die
quarantänisierte Alt-EXE entfernt. Ein noch laufender Altprozess oder eine Installation in einem anderen Profil
wird dabei nicht automatisch beendet oder verändert, sondern verhindert den Dienstmodus. Der registrierte
Desktop-Uninstaller bleibt für die übrigen Desktopdateien
verfügbar. Für die spätere Rückkehr nach Deinstallation des Dienstmodus wird der Desktopmodus neu installiert.

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

Plan, Seal und Apply des Desktopwechsels sind von der Diensttransaktion getrennte, dauerhaft belegte Phasen. Vor
der ersten SCM- oder Maschinenmutation schreibt der Öffnen-Client außerdem ein unveränderliches
`PREPARED`-Manifest unter
`%ProgramFiles%\E-Rechnungs-Pruefer-Dienst\.installer-state`. Es bindet Transaktions-ID, Desktop-Seal,
ursprüngliche SCM-Metadaten, Maschinenzustand und Zielzustand. Erst nachdem neuer Bundlebaum, stabiler Dienst,
Maschinenzustand und Healthcheck bewiesen sind, wird dort atomar `COMMIT_STARTED` veröffentlicht. Ein späterer
Setupstart reconciliert diese Belege vor dem normalen Preflight: vor `COMMIT_STARTED` ausschließlich zurück zur
exakten Baseline, danach ausschließlich vorwärts zum bereits bewiesenen Ziel. Fehlende, fremde, widersprüchliche
oder nicht eindeutig zuordenbare Belege beziehungsweise Bundle-/SCM-Zustände blockieren jede Recovery
geschlossen. Die Belege werden erst nach Desktopabschluss und Servicebereinigung entfernt.

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

Voraussetzungen sind Windows-x64-Python 3.13, Inno Setup 6 oder 7 und Netzwerkzugriff beim Vorbereiten der
gesperrten Komponenten:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . -r packaging\windows\requirements-build.txt
python scripts\prepare_windows_components.py
.\scripts\build_windows.ps1
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

## Automatisierte Paket- und Migrationstests

Alle folgenden Skripte verändern reale Installer-, Dienst-, Registry- und Tokenzustände. Sie dürfen
ausschließlich in einer sauberen, entbehrlichen Windows-VM beziehungsweise unter einer eigenen Testidentität
laufen. `-ConfirmIsolatedEnvironment` bestätigt diese Voraussetzung, hebt die Vorabprüfungen aber nicht auf.

```powershell
.\scripts\build_windows.ps1 -BuildElevatedMigrationTestInstaller
.\scripts\test_windows_package.ps1 -ConfirmIsolatedEnvironment
.\scripts\test_windows_service_package.ps1 -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext
.\scripts\test_windows_migration.ps1 -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext
```

`-BuildElevatedMigrationTestInstaller` erzeugt zusätzlich unter `build\windows\test-installer` einen ausschließlich
für den erhöhten, unbeaufsichtigten VM-Test bestimmten Dienst-Installer. Nur dieser Build enthält die interne
Freigabe für `/ALLOWELEVATEDTESTCONTEXT=1`; der produktive Installer unter `dist` enthält und akzeptiert diesen
Testpfad nicht. Der zusätzliche Test-Installer wird weder in das Prüfsummenmanifest noch in Release-Artefakte
aufgenommen. Beim signierten Vorab-Probelauf wird jedem Testskript zusätzlich `-RequireSignature` übergeben.

Der Desktoptest deckt Installation, Browser-/API-Authentifizierung, PDF, bytegetreuen XML-Export, KoSIT,
HKCU-Autostart, laufendes Update und Deinstallation ab. Der Diensttest prüft unter anderem Dienstkonto, ImagePath,
Starttyp, konfigurierte und durch erzwungenen Prozessabbruch ausgelöste Recovery, SCM-Zustände, reine
Loopback-Bindung, geschützte DACLs samt effektiven Rechten, Browser-IPC, den Global-Mutex, API-Tokenfälle,
Tokenpersistenz über Stop/Start und Update, einen absichtlich fehlgeschlagenen Update-Rollback sowie
den vollständigen Bundlebaum, die Entfernung veralteter Dateien, den manuellen Starttyp, einen frühen
Portkonflikt und Deinstallation mit Erhalt und ausdrücklicher Löschung von ProgramData. Mit `-RequireSignature` werden zusätzlich
die installierten eigenen EXEs und Installer geprüft. Eine konkrete Windows-Testidentität wird als zusätzlicher
Tokenleser provisioniert; der Test rotiert das Token bei gestopptem Dienst und weist nach, dass genau ihr
schreibfreier ACE über Rotation, Update und Neuinstallation erhalten bleibt.

Der Migrationstest installiert den veröffentlichten, signierten Desktopstand v1.3.0, aktiviert Autostart und
Backend und wechselt dann mit ausdrücklicher Tokenübernahme zum Dienst. Er prüft kontrolliertes Beenden,
Autostartentfernung, Tokenidentität, neue DACL, den Ausschluss eines parallelen Desktopbackends und zunächst einen
absichtlich fehlgeschlagenen Moduswechsel mit Wiederherstellung von Desktopprozess, Desktop-EXE und Autostart.
Nach Erfolg weist er die portunabhängige Entfernung der alten Backend-EXE nach. Ohne
`-DesktopSetup130` lädt er den offiziellen v1.3.0-Installer vom zugehörigen GitHub Release.

Auf einer Wegwerf-VM können zusätzlich zwei echte Prozessabbruch-Checkpoints gefahren werden:

```powershell
.\scripts\test_windows_migration.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext `
    -DesktopHardKillRecovery Immediate
.\scripts\test_windows_service_package.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext `
    -CommitHardKillRecovery Immediate
```

Der erste Helfer beendet ausschließlich den von ihm gestarteten, anhand PID und kanonischem EXE-Pfad
identifizierten Setup-Prozessbaum, nachdem Desktop-Seal und `Apply` nachgewiesen sind, aber noch kein
Dienst-`PREPARED`-Manifest und kein SCM-Dienst existieren. Derselbe Installer wird danach erneut gestartet; die
frühe Recovery muss den Desktopzustand zunächst vollständig zurückrollen. Der zweite Helfer unterbricht ein
Update erst nach einem hashgebundenen `COMMIT_STARTED`-Marker und startet denselben Installer erneut; dieser muss
den bereits committed Dienst ausschließlich vorwärts bereinigen. Beide Helfer bestehen nur, wenn der vollständig
geparste, DACL-geprüfte Marker nach dem harten Abbruch noch unverändert vorhanden ist. Ist das absichtlich kurze
Zeitfenster verpasst oder Setup bereits beendet, bricht der Test ab und meldet den Checkpoint ausdrücklich nicht
als ausgeführt.

CI verwendet frische Windows-Runner für diese zerstörenden Paketprüfungen. Sie ersetzen keine manuelle
Endabnahme.

### Reboot-Abnahme der persistenten Recovery

Ein realer Stromverlust oder Hypervisor-Reset wird bewusst nicht aus einem Testskript ausgelöst. Für die
zweistufige VM-Abnahme halten die Hard-Kill-Helfer mit `LeaveForReboot` den exakt verifizierten persistenten
Zustand fest und beenden sich anschließend absichtlich mit Exitcode `194`, also nicht als bestandener Gesamttest:

```powershell
.\scripts\test_windows_migration.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext `
    -DesktopHardKillRecovery LeaveForReboot

# Auf einer zweiten frischen VM beziehungsweise einem zweiten Snapshot:
.\scripts\test_windows_service_package.ps1 `
    -ConfirmIsolatedEnvironment -AllowElevatedMigrationTestContext `
    -CommitHardKillRecovery LeaveForReboot
```

Nach jedem Lauf:

1. Vor dem Neustart anhand der Skriptausgabe und Exitcode `194` bestätigen, dass der gewünschte Marker nach dem
   harten Setupabbruch erhalten blieb. Ein anderer Abbruch ist kein durchgeführter Checkpoint.
2. Die VM tatsächlich hart zurücksetzen oder ausschalten und erneut starten; keinen Snapshot auf den Zustand vor
   dem Checkpoint zurücksetzen.
3. Nach Anmeldung exakt denselben Testinstaller erneut mit
   `"/VERYSILENT"`, `"/SUPPRESSMSGBOXES"`, `"/NORESTART"`,
   `'/TASKS="systemstart"'` und `"/ALLOWELEVATEDTESTCONTEXT=1"` starten. Beim Desktopfall zusätzlich
   `"/MIGRATEDESKTOPTOKEN=1"` übergeben.
4. Beim Desktopfall einen laufenden, eigenen Dienst, das entfernte erwartete HKCU-Autostartziel, identische
   Inhalte von Desktop- und Dienst-API-Token sowie die Abwesenheit der quarantänisierten Desktop-EXE nachweisen.
   Beim Updatefall den laufenden eigenen Dienst, das unveränderte Token und die Abwesenheit der
   `commit-recovery-sentinel.txt` nachweisen.
5. In beiden Fällen die Abwesenheit von `service.new`, `service.rollback`, `service.obsolete`,
   `%ProgramFiles%\E-Rechnungs-Pruefer-Dienst\.installer-state` und
   `%ProgramData%\E-Rechnungs-Pruefer-Installer-State` prüfen. Ein verbliebener oder widersprüchlicher Zustand
   zählt nicht als erfolgreiche Recovery und darf nicht manuell gelöscht werden, bevor Diagnoseinformationen
   gesichert sind.

## Manuelle Windows-11-Abnahme vor Veröffentlichung

Vor einem öffentlichen Release ist das signierte Vorab-Artefakt auf einer sauberen, anschließend verworfenen
Windows-11-x64-VM zu prüfen:

1. Bundle-ZIP entpacken und Signaturen sowie SHA-256-Datei aller fünf eigenen Dateien und des ZIPs prüfen.
2. Desktopmodus einschließlich Tray, Standardbrowser und optionalem HKCU-Autostart prüfen.
3. Den Dienst mit verzögertem Systemstart installieren und Windows tatsächlich neu starten.
4. Vor der ersten Benutzeranmeldung über Dienststatus und technische Logs nachweisen, dass der Dienst erfolgreich
   gestartet und nur an `127.0.0.1` gebunden ist.
5. Nach Anmeldung den Öffnen-Client, API-Authentifizierung, PDF/XML und echte KoSIT-Annahme und -Ablehnung prüfen.
6. Falls der gesamte Node-RED-Ablauf vor Anmeldung gefordert ist, auch Node-RED unter der vorgesehenen
   Dienstidentität betreiben und den vollständigen Mailflow vor einer interaktiven Anmeldung abnehmen.
7. Update, Migration, beide Hard-Kill-/Reboot-Recovery-Richtungen und beide Deinstallationsvarianten sowie
   Defender/SmartScreen kontrollieren.

## Drittkomponenten

Die mitgelieferten Lizenz- und NOTICE-Dateien der offiziellen Archive bleiben im Bundle erhalten, soweit sie
Bestandteil der Archive sind. Ergänzende Angaben stehen in `THIRD_PARTY.md`. Vor kommerzieller Verwendung sind
insbesondere die aktuellen Bedingungen von Inno Setup und die Weitergabebedingungen aller gebündelten
Komponenten zu prüfen.
