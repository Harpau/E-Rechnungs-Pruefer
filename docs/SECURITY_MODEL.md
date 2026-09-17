# Sicherheitsmodell

## Schutzgüter

- Rechnungsinhalte und personenbezogene Daten
- Bank- und Steuerkennungen
- Original-XML und Prüfergebnisse
- lokales Dateisystem und Prozessumgebung
- Integrität der KoSIT-Konfiguration

## Vertrauensgrenzen

1. Uploads sind vollständig untrusted.
2. PDF-Anhänge und XML-Namen sind untrusted.
3. XML-Inhalte, Namespaces, Attribute und Textwerte sind untrusted.
4. Java-/KoSIT-Ausgaben und Berichtsdateien sind untrusted, bis sie sicher geparst wurden.
5. Browserausgabe muss alle Rechnungswerte escapen.
6. Downloads aus dem KoSIT-Installer erfolgen nur nach ausdrücklichem Benutzeraufruf. Für KoSIT Validator und
   XRechnung ist `packaging/kosit/components.lock.json` maßgeblich. Der Windows-Lock unter
   `packaging/windows/components.lock.json` spiegelt diese beiden Einträge und ergänzt die festgeschriebene
   Java-Laufzeit; der Build verifiziert alle SHA-256-Prüfsummen. Der zentrale KoSIT-Lock pinnt Validator 1.6.3
   und die XRechnung-3.0.2-Konfiguration 2026-08-31 mit CEN-Regeln 1.3.16 und
   XRechnung-Schematron 2.6.0.

## Wesentliche Bedrohungen und Kontrollen

### XML External Entity und DTD

Kontrollen: Vorabprüfung auf DTD/ENTITY, `resolve_entities=False`, `load_dtd=False`, `no_network=True` und keine Recovery-/Huge-Tree-Modi.

### ZIP Slip bei XRechnung-Konfiguration

Der Installer prüft jeden ZIP-Zielpfad vor dem Extrahieren gegen das Zielverzeichnis.

### PDF-Anhangsauswahl

Kennwortgeschützte PDFs werden abgelehnt. Verschlüsselte PDFs, die sich mit einem leeren Passwort entschlüsseln lassen, dürfen verarbeitet werden. Es werden nur Anhänge verarbeitet, deren Bytes wie XML aussehen. Bekannte Rechnungsnamen erhalten Priorität. Andere Anhänge werden nur als Metadaten aufgeführt und nicht ausgeführt.

### Ressourcenverbrauch

Der HTTP-Verarbeitungspfad schützt `/api/analyze`, `/api/xml`, `/api/report` und `/api/report/pdf` gemeinsam.
Begrenzte und eindeutige HTTP-Header werden vor der bisherigen Browser-/Bearer-Authentifizierung geprüft.
Erst danach werden Requestrepräsentation und Konfiguration validiert und einer von zwei Auftragsplätzen
reserviert. Eine abgewiesene Anfrage liest keinen Body und löst deshalb kein `100 Continue` aus. Es gibt keine
automatische FastAPI-Multipart-Verarbeitung oder temporäre Upload-Spooldatei. Der Receivekanal hat jeweils genau
einen Leser; nach vollständigem Body übernimmt dieser Controller die Disconnectüberwachung.

| Grenze | Festgelegter Standard |
|---|---|
| Datei | 25 MiB; `MAX_UPLOAD_BYTES` darf nur auf eine positive kleinere Grenze gesetzt werden |
| Gesamter Multipart-Body / Nicht-Dateianteil | Dateilimit + 64 KiB / höchstens 64 KiB |
| Dateiparts | genau eine Datei; nur die zur Route gehörenden optionalen Felder, keine Duplikate |
| HTTP-Header nach ASGI-Übergabe | 64 Header, zusammen 16 KiB, Einzelwert 8 KiB; Content-Type 1 KiB |
| Partheader / Dateiname / Feldwert / Parserfeed | 4 KiB / 1.024 Byte / 64 Byte / 64 KiB |
| Upload | 120 s insgesamt, höchstens 15 s bis zum nächsten Receiveereignis |
| Prozessstart / reine Pythonphase | 15 s / 30 s; KoSIT-Wartezeit zählt nicht zur Pythonphase |
| Verarbeitung insgesamt | 60 s + angeforderte KoSIT-Frist; standardmäßig 120 s, höchstens 360 s |
| KoSIT-Frist | 1..300 s, Standard 60 s |
| Abbruch-Cleanup / Antwortversand | 5 s / 30 s |
| JSON / HTML / PDF / XML-Ausgabe | 128 MiB / 128 MiB / 64 MiB / 25 MiB |
| Java stdout / stderr / gesammelte VARL-Dateien | 2 MiB / 2 MiB / 2 MiB insgesamt bei höchstens 8 Kandidaten |

Ein Platz bleibt von Uploadbeginn bis zum belegten Prozessende und vollständigem Antwortversand beziehungsweise
Abbruch belegt. Volle Kapazität führt ohne Warteschlange zu `503 analysis_capacity_error`, auch beim XML-Export;
der Healthcheck bleibt unabhängig. Nicht bestätigtes Cleanup sperrt den betroffenen Platz. Diese Grenzen gelten
**je Backendprozess**: Mehrere Uvicorn-Worker oder mehrere Instanzen vervielfachen Kapazität und Speicherbedarf.
Die Antwort wird vollständig innerhalb ihres Budgets gesammelt und der kleine Metadatenvertrag geprüft, bevor
HTTP 200 beginnt. Nach begonnenem Versand kann ein Disconnect/Sendetimeout nur noch die Verbindung beenden.

Der Backend-Controller startet und besitzt Python-Worker, IPC-Supervisor, optionalen Java-Launcher und auf macOS
den unabhängigen Wächter direkt. Rechnungsbytes werden erst nach bestätigter Start-/Limitbindung freigegeben.
Die einmal im Parent gewählten Profile werden als konkrete Werte an die Rollen übergeben. Linux erhält
2.048 MiB für vertrauenswürdige Python-Imports, danach 768 MiB für den Worker und 512 MiB für den Supervisor.
macOS erhält dafür 4.096 / 1.536 / 1.024 MiB. Java erhält 4.096 MiB bei 512 MiB Java-Heap, der macOS-Wächter
separat 64 MiB. Auf POSIX sind diese Werte zusätzlicher virtueller Adressraum über einer geprüften Basis;
insbesondere macOS besitzt große gemeinsame Adressabbildungen. Das ist weder eine RSS-Grenze noch eine Zusage
über den physischen RAM-Verbrauch. Das macOS-Profil wurde anhand wiederholter vollständiger synthetischer
25-MiB-Exporte und CII-PDF-Berichte kalibriert; die kleineren Profile verarbeiteten diese regulären Fälle
nicht zuverlässig. Die gemessenen RSS-Spitzen dieser sechs Proben waren rund 142 MiB für den Worker und
23 MiB für den Supervisor; sie sind keine Zusage für beliebige Dokumente.

Die vertrauenswürdige Adressraumbasis wird vor Rechnungsinput gemessen und nach derselben festen Grenze beim
Start, nach Bibliotheksimports und im READY-Nachweis geprüft: höchstens 1 GiB unter Linux, 64 GiB unter macOS
x86_64 und 512 GiB unter macOS arm64. Unbekannte macOS-Architekturen werden abgewiesen. Diese Obergrenzen
begrenzen die zulässige Basis, nicht den Arbeitszuschlag: Das tatsächlich gesetzte Limit bleibt die gemessene
Basis plus dem oben genannten unveränderten Rollenbudget. Rund 392 GiB virtueller Basis auf dem ARM64-CI-Runner
sind kein entsprechender RAM-Verbrauch und keine freie Speicherreserve. Die macOS-CI verlangt vor dem
Testkatalog begrenzte mmap- und Heap-Proben zur tatsächlichen Limitdurchsetzung; die neue ARM64-Grenze allein
belegt weder diese Durchsetzung noch eine bestandene native ARM64-Abnahme.

Windows verwendet dagegen Job-/Commitgrenzen: Worker 2.048 MiB beim Import und danach 768 MiB, Supervisor
512 MiB und zusammen 4.096 MiB für Java-Launcher und JVM. Der äußere Auftragsjob ist insgesamt auf 6,5 GiB
begrenzt. Diese Windowswerte benötigen ihre eigene native Kalibrierung; aus einem macOS-AS-Test folgt kein
Windows-Commit- oder Linux-Nachweis. Java-Heap und gesamter JVM-Bedarf sind unterschiedliche Größen.
Die äußere monotone Frist bleibt maßgeblich; ein Thread-Timeout allein beendet keine Verarbeitung.

Der XML-Preflight greift weiterhin vor dem vollständigen Baumaufbau. Bei Hybrid-PDFs gilt das Dateibudget für die
ausgewählte XML und die Summe aller dekodierten Anhänge, bei höchstens 100 Anhängen. Jede Dekoderstufe verwendet
das verbleibende Gesamtbudget; bei Nullrest startet kein weiterer Dekoder. Strukturstreams verwenden unabhängig
davon höchstens 25 MiB. Der Seitenbaum ist auf 10.000 Einträge (auch innere Knoten) und Tiefe 64 begrenzt. Diese
Grenzen wählen keine Anhänge stillschweigend ab und führen keine neue fachliche Positionsgrenze ein. Sie ersetzen
nicht das native Speicher-/Zeitlimit: Nicht jeder Dekoder verhindert jede Allokation bereits vor seinem Callback.

Geltungsbereich ist der HTTP-Aufruf über den Manager. Direkte Bibliotheksaufrufe und CLI-Analysen erhalten damit
nicht automatisch native Prozessisolation. Die neue Architektur ist keine vollständige Betriebssystem-Sandbox;
Dateisystem-/Netzwerkrechte, JVM-/Containerquoten des Deployments und ein gesamtsystemweites Speicherbudget
werden dadurch nicht zugesagt. Das gilt insbesondere für Java-Tempdaten trotz geschützter Pfade und begrenzter
Ein-/Ausgaben. Die gesonderte Betriebshärtung ist nicht Teil dieser Änderung. Offene Dependency-/OS-Befunde bleiben
unverändert freigaberelevant; Prozessgrenzen begründen keine CVE-Ausnahme.

Die nativen Windows-/Linux-, installierten Dienst-/Desktop- und Frozen-Nachweise für diese neue Architektur
müssen vor Freigabe nach [`RELEASE.md`](RELEASE.md) erbracht werden. Ein bestandener Unit- oder macOS-Quelltest
ist kein Nachweis für diese anderen Auslieferungsformen.
Der [Abnahmestand mit nativen Messwerten](UPLOAD_WORKER_ACCEPTANCE.md) dokumentiert die bestandenen
Teilprüfungen und die noch offene Windows-Paketabnahme; die technische Gesamtfreigabe bleibt ausstehend.

### Cross-Site Scripting

Jinja2 escaped standardmäßig; die JavaScript-Oberfläche verwendet `escapeHtml` für Rechnungswerte. Änderungen an `innerHTML` müssen sicherstellen, dass jeder untrusted Wert vorab escaped wird. Die Content Security Policy verhindert fremde Skripte und Objekte.

### Geschlossener Analysevertrag und Feldabdeckung

`POST /api/analyze` lässt das Ergebnis im begrenzten Worker gegen das geschlossene Analyseschema 2 validieren.
Der HTTP-Parent deserialisiert dieses vollständige Modell nicht erneut. Zusätzliche Parser- oder
Validatorfelder dürfen nicht versehentlich in die API gelangen. Die explizite Abbildung deckt Dokument,
Capabilities, Parteien, Rollen, Zeiträume, Referenzen, Positionen, Nachlässe/Zuschläge, Steuern, Summen,
Zahlungsanweisungen, Quelle, technische Darstellung und Laufzeit ab. Nicht verstandene XML-Daten werden nicht
als verstandene Fachfelder ausgegeben, bleiben aber in der technischen Feldliste, den XML-Textansichten und im
bytegetreuen Export verfügbar.

Diese Feldabdeckung ist keine Anonymisierung. Namen, Adressen, Steuerkennungen, IBANs, Referenzen und andere
Rechnungsinhalte können im autorisierten Analyseergebnis enthalten sein. API-Antworten tragen `Cache-Control:
no-store`; Integrationen dürfen sie trotzdem nicht in gewöhnliche Logs, Fehlertexte oder ungeschützte
Zwischenspeicher kopieren.

### Kartenmaskierung und Originalexport

Erkannte Kartenkontokennungen werden an der Schema-2-Grenze auf höchstens die letzten vier Zeichen maskiert.
Dieselben Rohwerte werden aus `technical.fields`, `technical.source_xml` und `technical.pretty_xml` redigiert.
Browser-, HTML- und PDF-Renderer maskieren Kartenkennungen zusätzlich defensiv, falls ihnen entgegen dem Vertrag
ein unmaskierter Wert übergeben wird.

Der Endpunkt `POST /api/xml` ist ausdrücklich ausgenommen: Er muss die ausgewählte Rechnungs-XML bytegetreu
zurückgeben und kann daher auch die ursprüngliche Kartenkennung und alle anderen Rechnungsdaten enthalten.
Maskierte Analyse- und Berichtsdarstellungen dürfen nicht als Zusage eines anonymisierten Originalexports
verstanden werden.

### Strikte Syntax- und Decimal-Behandlung

Die Syntaxerkennung verlangt unterstütztes Wurzelelement und exakten Namespace. Auch fachliche Kindelemente
werden URI-qualifiziert ausgewählt; fremde, vertauschte oder nur lokal gleichnamige Elemente werden nicht als
UBL-/CII-Werte interpretiert. Legitime Erweiterungen bleiben im technischen Anhang und Originalexport erhalten.
Die XML-Decimal-Konvertierung akzeptiert nur endliche
Werte im vorgesehenen Dezimalraum; insbesondere werden `NaN`, Unendlichkeiten, Exponenten, Dezimalkomma und
freie Texte nicht in Rechenwerte umgedeutet. Dadurch entstehen aus untrusted Eingaben weder nichtendliche
Rechenoperationen noch irreführende Folgefehler auf Basis erfundener Ersatzwerte. Pro Rechenoperand gelten
höchstens 4.096 Dezimalziffern. Die Arbeitspräzision wird aus maximaler Operandenspanne, Operationsbreite und
Anzahl der Operanden abgeleitet, bleibt aber hart begrenzt; größere Werte enden kontrolliert und ohne Echo des
Rohwerts mit `422 invoice_input_error`. Damit kann eine nichtterminierende Division den Decimal-Kontext nicht aus
der Summe vieler Eingabefelder auf Millionen Stellen vergrößern.

### Lokale Windows-Webserver

Desktop und Dienst binden ihren festen Port ausschließlich auf `127.0.0.1`. Ein maschinenweiter, explizit
geschützter Backend-Mutex und die exklusive Reservierung des festen Ports verhindern den parallelen Betrieb.
Beide Kontrollen schlagen bei einem Konflikt geschlossen fehl; es gibt keinen Ausweichport. Der tokenfreie
Healthcheck akzeptiert nur zulässige Loopback-Hostheader und veröffentlicht weder Dateipfade noch konkrete
KoSIT-Konfigurationsprobleme. Es gibt bewusst keinen HTTP-Shutdown-Endpunkt.

Desktop- und Dienstmodus werden nicht automatisch ineinander überführt. Der Dienst-Installer verändert weder
Desktopdateien noch HKCU-Autostart oder benutzerbezogene Tokens; der Desktop-Installer verändert weder SCM-Dienst
noch geschütztes ProgramData. Vor einem Wechsel muss die aktive Betriebsart regulär deinstalliert werden.
Der maschinenweite Backend-Mutex und die feste Portreservierung bleiben eine zweite Laufzeitgrenze, ersetzen aber
nicht den gegenseitigen Installationsausschluss.

Der Dienst-Preflight inventarisiert den Gegenmodus read-only in allen registrierten lokalen und Entra-ID-Profilen.
Er prüft den Standardinstallationsordner, Uninstall-Key und Autostart. Geladene Hives werden ausschließlich über
`HKEY_USERS` gelesen. Für ein abgemeldetes Profil muss genau ein no-follow geprüfter `NTUSER.DAT`- oder
`NTUSER.MAN`-Hive vorhanden sein. Der Scanner öffnet diese Datei mit einem gegen Schreiben und Löschen gesperrten
Lesehandle, begrenzt ihre Größe und wertet einen einmaligen Speicher-Snapshot mit der exakt gepinnten
Offline-Registry-Komponente Regipy aus. Diese Auswertung läuft in einem eigenen Hilfsprozess mit fester
30-Sekunden-Grenze je Hive und einer 60-Sekunden-Gesamtgrenze je Inventur; Timeout, Prozessfehler oder
Speichererschöpfung blockieren die Installation geschlossen.
Der Hive wird weder systemweit noch privat gemountet; insbesondere wird `RegLoadAppKeyW` nicht verwendet und
keine zweite Hive-Datei angelegt.

Dateiidentität, REGF-Signatur, Header-Prüfsumme, Sequenznummern, die vollständige HBin-Kette, der explizite
Root-Key-Verweis und die für die Inventur verfolgten Strukturzähler müssen vor beziehungsweise während der
Auswertung konsistent bleiben.
Fehlende, doppelte, umgeleitete, veränderte, nicht kanonische oder nicht vollständig lesbare Profile und Hives
führen zum geschlossenen Abbruch. Dadurch bleiben auch
benutzerdefinierte Installationspfade abgemeldeter Desktop-Altinstallationen erkennbar. Der Scanner besitzt keinen
Migrations- oder Bereinigungspfad und verändert kein fremdes Profil. Eine laufende Instanz wird zusätzlich über
Prozess und Desktop-Mutex erkannt.

API-Tokens bleiben absichtlich an ihre Betriebsart gebunden. Das Diensttoken wird nicht aus dem Desktopprofil
gelesen oder kopiert; die frühere Option `/MIGRATEDESKTOPTOKEN=1` wird nicht unterstützt. Ein bei der
Dienstdeinstallation ausdrücklich erhaltenes, weiterhin geschützt inventarisiertes ProgramData-Verzeichnis ist
allein kein installierter Gegenmodus und darf die Desktopinstallation nicht blockieren. Der Desktop-Installer
darf diesen Maschinenzustand weder lesen noch verändern. Eine spätere Dienstneuinstallation übernimmt das
erhaltene Diensttoken ausschließlich über den geschützten Dienstzustand.

Unvollständige v1.4.0-Migrations-, Transfer-, Seal-, Quarantäne- und daran gebundene Alttransaktionszustände liegen
außerhalb des unterstützten Upgradepfads. Ein neuer Installer darf daraus keine Pfade, Identitäten oder Tokens
ableiten und keine scheinbare Recovery durch selektives Löschen unbekannter Marker erzwingen. Paket- und
Freigabetests beginnen deshalb auf einer sauberen Wegwerf-VM ohne solche Altzustände.

#### Desktopmodus

Der Desktop-Launcher erzeugt pro Prozess ein zufälliges Browser-Sitzungstoken. Ein Startlink setzt ein
`HttpOnly`-/`SameSite=Strict`-Cookie und entfernt das Token durch Weiterleitung aus der sichtbaren URL. Weitere
Browseranfragen benötigen dieses Cookie; Host und bei schreibenden Browseranfragen der Origin werden geprüft.
Die Startseite ist `no-store`; HTML, JavaScript und CSS sind über eine gemeinsame Inhaltsrevision gekoppelt.
Cookieauthentifizierte UI-API-Aufrufe mit fehlender oder veralteter Revision enden erst nach erfolgreicher
Authentifizierung und Originprüfung mit einem kontrollierten `409`, damit ein offenes Alt-Tab keine neue
Serverantwort in ein inkompatibles DOM rendert. Bearer-authentifizierte Automatisierungen sind ausgenommen.
Wurde der Prozess neu gestartet, ist das alte Sitzungscookie absichtlich nicht mehr gültig; dieser Fall endet
bereits mit `403 desktop_session_error` und demselben Wiederöffnungshinweis. Die UI-Prüfung schwächt die
Authentifizierungsreihenfolge damit nicht ab.
Die Laufzeitdatei unter `%LOCALAPPDATA%` enthält Port, Prozess-ID und das kurzlebige Browser-Token, ist durch die
Rechte des angemeldeten Windows-Kontos geschützt und wird beim normalen Beenden beziehungsweise bei der
Deinstallation entfernt.

Ein davon getrenntes API-Token liegt dauerhaft unter
`%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt`. Bearer-Authentifizierung gilt für die fachlichen
`/api/*`-Endpunkte und gewährt keinen Zugriff auf Startseite oder Desktop-Bootstrap; `/api/health` bleibt als
lokaler Healthcheck tokenfrei. Das Token besteht ausschließlich aus URL-sicherem ASCII, erscheint weder in URLs
noch in der Laufzeitdatei und wird bei der Desktop-Deinstallation entfernt.
Nicht-ASCII-Eingaben werden kontrolliert abgewiesen. Prozesse desselben kompromittierten Benutzerkontos liegen
weiterhin außerhalb der Schutzgrenze. Desktop-Installer und -Uninstaller verwenden zur kontrollierten Beendigung
nur das benannte lokale Desktop-Shutdown-Ereignis.

#### Dienstmodus und Maschinenzustand

Ein vom Backend-Mutex getrennter, globaler Setup-/Uninstall-Mutex serialisiert alle erhöhten Installations-,
Update-, Recovery- und Deinstallationsläufe sitzungsübergreifend. Er wird atomar ohne Wartefenster erworben und
bis nach Commit, Rollback oder Cleanup gehalten. Belegung, Zugriffsfehler und unbekannte Warteergebnisse führen
zum geschlossenen Abbruch; nach einem abgebrochenen Vorbesitzer läuft unter der übernommenen Sperre zuerst die
persistente Recovery.

Unveränderliche Dienstdateien liegen unter `%ProgramFiles%`; Konfiguration, Token und technische Logs unter
`%ProgramData%\E-Rechnungs-Pruefer`. Der Dienst läuft als `NT AUTHORITY\LocalService`, nicht als `LocalSystem`,
und aktiviert `NT SERVICE\ERechnungsPrueferService` als dienstspezifischen SID. Geschützte, nicht geerbte DACLs
begrenzen den Maschinenzustand auf diesen SID, `SYSTEM` und lokale Administratoren. Insbesondere erhalten
`Everyone`, `Authenticated Users`, interaktive Sammelidentitäten und Gruppen keinen pauschalen Zugriff auf das
Token. Eine tatsächlich ermittelte konkrete Node-RED-Benutzer-, Computer-/gMSA- oder dienstspezifische SID kann
ausdrücklich mit Leserechten provisioniert werden.

Bestätigt ein Administrator in Windows Explorer den Zugriff auf ein geschütztes Dienstverzeichnis, kann Explorer
dort einen zusätzlichen expliziten Benutzer-ACE hinterlassen. Für die Wiederanlauf- und
Neuinstallationskompatibilität wird ausschließlich auf dem ProgramData-Stamm und dem Logverzeichnis genau ein
solcher ACE akzeptiert: Die SID muss ein direktes Benutzer-Mitglied der lokalen Administratorgruppe sein und der
ACE muss exakt expliziten Vollzugriff mit `OI|CI` enthalten. Dateien, Gruppen-SIDs, andere Masken oder Flags,
mehrere Zusatzidentitäten und nicht vollständig auflösbare Mitgliedschaften bleiben geschlossen abgewiesen. Der
Dienst normalisiert den Stamm vor dem eigentlichen Start und die Logpfade vor Öffnung des Logs; eine
erhöhte Setup-Vorprüfung normalisiert denselben Zustand vor dem Lesen der Maschinenkonfiguration.

Technische Logobjekte können beim Erzeugen zunächst `LocalService` als Besitzer erhalten. Weil dieses Konto von
anderen Diensten geteilt wird, enthält ihre DACL zusätzlich einen exakt geprüften `OWNER RIGHTS`-ACE: Er nimmt dem
Besitzer das implizite `WRITE_DAC`, während nur der dienstspezifische SID den benötigten Vollzugriff behält.

Konfiguration und Token werden atomar über eine bereits endgültig geschützte temporäre Datei und unter Windows mit
einer Write-through-Verzeichnisumbenennung veröffentlicht. Die
streng validierte Konfiguration enthält keine Bind-Adresse; diese bleibt fest auf Loopback. Tokenrotation ist nur
bei gestopptem Dienst erlaubt. Updates und die Standarddeinstallation erhalten den Maschinenzustand. Eine
Deinstallation löscht ihn nur nach einer klaren Benutzerentscheidung. Auch dann wird der Known-Folder-Pfad vor
der Löschung vollständig neu inventarisiert: Nur die bekannten Konfigurations-, Token- und Logrotationsdateien
mit vertrauenswürdigem Besitzer und enger DACL werden einzeln entfernt. Unbekannte Einträge, Reparse-Points,
Junctions, Hardlinks oder verbreiterte Rechte führen zum geschlossenen Abbruch; ein rekursives Löschen findet
nicht statt. Der transiente KoSIT-`runtime`-Baum gehört nicht zum beibehaltenen Maschinenzustand: Exakt passende,
vollständig inventarisierte Crashreste werden beim nächsten Dienststart und bei jeder Deinstallation unabhängig
von der Auswahl für Konfiguration, Token und Logs entfernt.

Der Maschinenpfad stammt aus der Windows-Known-Folder-API und nicht aus `PROGRAMDATA` in der Prozessumgebung.
Besitzer, geschützte DACL und konkrete Tokenleser werden vor einer Übernahme positiv geprüft; administrative
Initialisierung normalisiert Verzeichnis, Konfiguration und Token auf `BUILTIN\Administrators` als Besitzer.
Reparse-Points, Junctions und Hardlinks an Dienstdateien werden vor Lesen, Ersetzen oder ACL-Änderung abgewiesen.
Updates aktivieren einen vollständig neu entpackten Baum atomar und behalten den alten Baum bis zum Commit für
Rollback. SCM-Metadaten werden nicht über Registry-Schreibzugriffe, sondern über die SCM-APIs gesichert und
restauriert. Auch die Deinstallation veröffentlicht vor ihrer ersten SCM-Mutation einen getrennten,
administratorgeschützten Beleg mit vollständiger Baseline und ursprünglichem RUNNING-Zustand. Nach einem Abbruch
darf nur der Deinstallations-Reconciler diesen Zustand restaurieren beziehungsweise eine bereits abgeschlossene
SCM-Löschung bestätigen; ein Installer wird bei vorhandenem Beleg vor jeder Recovery oder neuen Transaktion
geschlossen abgewiesen.

Ein Prozessabbruch, Stromverlust oder Neustart macht den Setup-Exitcode unzuverlässig. Deshalb beginnt jede
Dienstmutation erst nach einem atomar veröffentlichten, unveränderlichen service-only `PREPARED`-Manifest.
Solange kein ebenfalls atomarer
`COMMIT_STARTED`-Marker vorliegt, darf ein Folgesetup nur die exakt belegte SCM-, Bundle- und
Maschinenzustands-Baseline restaurieren. Nach `COMMIT_STARTED` darf es ausschließlich den bereits verifizierten
Zielzustand vorwärts bereinigen. Der Folgelauf reconciliert vor seinem normalen Preflight. Fremde Dienstmetadaten,
instabile SCM-Zustände, unbekannte Bundlekombinationen, Hash-/Transaktionsabweichungen oder verwaiste
nichtterminale Belege führen ohne Mutation zum geschlossenen Abbruch. Die nativen
Hard-Kill-Tests und die manuelle Reboot-Abnahme sind in [`WINDOWS_PACKAGE.md`](WINDOWS_PACKAGE.md) beschrieben.

Der Dienst öffnet aus Session 0 weder Tray, Browser noch MessageBox. Ein interaktiver Öffnen-Client spricht über
eine nur lokale Named Pipe mit einem kleinen, versionierten Protokoll. Die Pipe weist Remoteclients ab, prüft eine
interaktive Sitzung und ist mit expliziten Windows-Zugriffsregeln geschützt; der Client ordnet ihren Serverprozess
dem vom SCM registrierten Dienst zu. Eine dauerhaft offene erste Instanz verhindert Namensübernahme zwischen
Anfragen; die Client-DACL enthält kein Recht zum Erzeugen weiterer Pipe-Instanzen. Die Antwort enthält nur einen zufälligen, höchstens 60 Sekunden gültigen und
einmal nutzbaren Browserbootstrap. Bevor der Dienst die Verbindung trennt, bestätigt der Client den Empfang der
exakten Antwortbytes innerhalb derselben begrenzten Austauschfrist; erst danach wird der Pipepuffer geleert.
Dieser Bootstrap wird gegen ein zeitlich begrenztes
`HttpOnly`-/`SameSite=Strict`-Cookie getauscht. Das dauerhafte Bearer-Token erscheint weder in Pipe-Nachrichten,
URL, Browser-Speicher, Cookie noch normalen Logs. Die Tabellen sind auf 32 ausstehende Bootstraplinks und 128
aktive Browsersitzungen begrenzt; der jeweils älteste Eintrag wird bei voller Kapazität verdrängt.

SCM-Kommandos steuern Start und Stopp. Der Server schließt zuerst die HTTP-Auftragsannahme und signalisiert
Upload-, Prozess- und Antwortaufträgen den Abbruch, bevor Uvicorn auf ihre Beendigung wartet. Die neuen
Rollenkinder erhalten zusätzlich ihre auftragsbezogenen Jobbindungen bereits bei der Erzeugung.
Der HTTP-Backendprozess hält die einzigen Handles der äußeren Auftragsjobs und der Rollenjobs. Jede Rolle wird
bereits bei Erzeugung in diese Jobs aufgenommen; der JVM-Prozess gehört zum Job seines gebundenen Launchers.
Beim Stoppen werden Listener und IPC geordnet geschlossen und alle Auftragsprozesse innerhalb der begrenzten
Bereinigung beendet. Ein harter Dienstabbruch schließt die Parent-Handles der Kill-on-close-Jobs und beendet
deren Prozesse. Der neue HTTP-Pfad setzt keinen nachträglichen Selbstbeitritt des SCM-Hosts zu einem Job voraus. Konsolenausgabe und Prüfbericht
werden bereits beim Lesen durch feste Bytebudgets begrenzt. Die temporäre Rechnungs-XML wird exklusiv angelegt
und nach bestätigtem Ende aller zugreifenden Prozesse gelöscht. Bei unbestätigtem Prozesscleanup bleiben der
Auftragsplatz gesperrt und der gebundene Tempkontext zur sicheren Klärung erhalten; Dateien unter einer
möglicherweise noch aktiven JVM werden nicht vorzeitig gelöscht. Sie verwendet unter Windows ausdrücklich kein
Delete-on-close, weil der dafür erforderliche Delete-Share-Modus den Datei-Open des Java-Prozesses verhindern kann.
Ein hartes Ende des Backendprozesses kann deshalb einen geschützten KoSIT-Tempbaum hinterlassen, obwohl die
Prozessbindung seine Kinder beendet. Die Anwendung behauptet für diesen Fall keine nachträgliche Dateilöschung.
Eine Bereinigung benötigt den belegten Prozessabschluss und eine Prüfung von Herkunft, Eigentümer, Rechten und
Links des exakten Tempbaums; fremde oder nicht eindeutig zuordenbare Verzeichnisse werden nicht entfernt.
Im Dienstmodus wird zuerst der private ProgramData-Elternpfad mit seiner administrativen, service-spezifischen
DACL erneut verifiziert. Darunter wird der gesamte zufällige KoSIT-Tempbaum atomar mit einer geschützten,
vererbbaren DACL für Service-SID, `SYSTEM` und Administratoren sowie einem begrenzenden `OWNER RIGHTS`-ACE
angelegt. Damit können andere Prozesse unter dem gemeinsam genutzten `LocalService`-Konto den Baum weder
umbenennen oder ersetzen noch Rechnungs-XML beziehungsweise VARL-Berichte lesen. Nach einem unkontrollierten
Betriebssystem- oder Prozessabbruch kann allein dieser geschützte Tempbaum kurzzeitig zurückbleiben. Vor dem
nächsten Dienstbetrieb und bei jeder Deinstallation wird er nur nach vollständiger Owner-, DACL-, Hardlink-,
Reparse-Point- und Objektinventur entfernt; Abweichungen führen zum geschlossenen Abbruch.
Ein Timeout oder erzwungenes Prozessende ist ein technischer Fehler und niemals eine fachliche Rechnungsablehnung.
Normale Dienstlogs enthalten weder Tokens, Authorization-Header, Rechnungsbytes noch sensible Rechnungsfelder.

### Pfad- und Dateinamenmanipulation

Upload- und Downloadnamen werden mit `Path(...).name` und einer Zeichen-Whitelist bereinigt. Temporäre
KoSIT-Dateien bleiben unter einem neu angelegten, zufälligen Tempverzeichnis und werden erst nach bestätigtem
Ende aller zugreifenden Prozesse entfernt; im Dienstmodus liegt dieser Baum unter dem verifizierten privaten ProgramData-Verzeichnis
und ist bereits ab seiner atomaren Erstellung durch die service-spezifische DACL geschützt.

### Falsche Validierungsentscheidung

Ein Prozessfehler ohne validen VARL-Bericht ist kein Rechnungsurteil. Eine vorhandene `accept`/`reject`-Entscheidung im Bericht ist maßgeblich und wird gegen den Rückgabecode plausibilisiert.

### Geheimnisse und echte Rechnungen im Repository

`.gitignore`, Release-Filter und `AGENTS.md` schließen lokale Konfigurationen, KoSIT-/Java-Dateien, Download-Caches, PDFs, Schlüssel und nicht freigegebene XML-Dateien aus. Die Schutzwirkung ersetzt keine Review von `git status` und Release-Inhalten. Der Windows-Build nimmt ausschließlich die gesperrten Komponenten in sein eigenes Endbenutzerartefakt auf.

## Nicht abgedeckt

- netzwerk- oder mehrbenutzerfähige Benutzer-/Rollen-Authentifizierung, Autorisierung oder Mandantentrennung
- Malware-Scanning beliebiger PDF-Inhalte
- digitale Signaturprüfung
- vollständige Betriebssystem-Sandbox, JVM-/Container-Deploymentquoten oder Hardware-Isolation des Java-Prozesses
- native Workergrenzen bei direkten Bibliotheks-/CLI-Aufrufen außerhalb des HTTP-Managers
- Schutz gegen einen bereits kompromittierten lokalen Rechner
- rechtssichere Langzeitarchivierung
