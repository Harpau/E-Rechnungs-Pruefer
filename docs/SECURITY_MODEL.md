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
6. Downloads aus dem KoSIT-Installer erfolgen nur nach ausdrücklichem Benutzeraufruf. Der Windows-Build lädt ausschließlich festgeschriebene Komponenten und verifiziert ihre SHA-256-Prüfsummen.

## Wesentliche Bedrohungen und Kontrollen

### XML External Entity und DTD

Kontrollen: Vorabprüfung auf DTD/ENTITY, `resolve_entities=False`, `load_dtd=False`, `no_network=True` und keine Recovery-/Huge-Tree-Modi.

### ZIP Slip bei XRechnung-Konfiguration

Der Installer prüft jeden ZIP-Zielpfad vor dem Extrahieren gegen das Zielverzeichnis.

### PDF-Anhangsauswahl

Kennwortgeschützte PDFs werden abgelehnt. Verschlüsselte PDFs, die sich mit einem leeren Passwort entschlüsseln lassen, dürfen verarbeitet werden. Es werden nur Anhänge verarbeitet, deren Bytes wie XML aussehen. Bekannte Rechnungsnamen erhalten Priorität. Andere Anhänge werden nur als Metadaten aufgeführt und nicht ausgeführt.

### Ressourcenverbrauch

Uploadgröße, technische Zeilenanzahl und KoSIT-Laufzeit sind begrenzt. Bei Hybrid-PDFs gilt `MAX_UPLOAD_BYTES` sowohl für die ausgewählte Rechnungs-XML als auch für die Summe der dekodierten Anhänge; zusätzlich werden höchstens 100 eingebettete Dateien verarbeitet. Das Dekompressionslimit von pypdf 6 bildet eine weitere Obergrenze. Da solche Prüfungen nicht jede Speicherallokation vor dem Dekodieren verhindern können, sind für den Netzwerkbetrieb weiterhin Prozess-, Speicher- und Parallelitätslimits notwendig.

### Cross-Site Scripting

Jinja2 escaped standardmäßig; die JavaScript-Oberfläche verwendet `escapeHtml` für Rechnungswerte. Änderungen an `innerHTML` müssen sicherstellen, dass jeder untrusted Wert vorab escaped wird. Die Content Security Policy verhindert fremde Skripte und Objekte.

### Lokale Windows-Webserver

Desktop und Dienst binden ihren festen Port ausschließlich auf `127.0.0.1`. Ein maschinenweiter, explizit
geschützter Backend-Mutex und die exklusive Reservierung des festen Ports verhindern den parallelen Betrieb.
Beide Kontrollen schlagen bei einem Konflikt geschlossen fehl; es gibt keinen Ausweichport. Der tokenfreie
Healthcheck akzeptiert nur zulässige Loopback-Hostheader und veröffentlicht weder Dateipfade noch konkrete
KoSIT-Konfigurationsprobleme. Es gibt bewusst keinen HTTP-Shutdown-Endpunkt.

Beim Wechsel zum maschinenweiten Dienst wird die bekannte v1.3-Desktopinstallation des aufrufenden Kontos
quarantänisiert. Zusätzlich inventarisiert der erhöhte Preflight laufende Altprozesse in allen Sitzungen sowie
registrierte und standardmäßige Installationspfade aller lokalen Benutzerprofile einschließlich Entra-ID-Profilen.
Für den Austausch mit dem ursprünglichen, nicht erhöhten Benutzer legt das erhöhte Setup einen kurzlebigen
Transferbaum unter
`%ProgramData%\E-Rechnungs-Pruefer-Installer-Transfer\<Setup-ID>` an. Stamm, Blatt und der dort bereitgestellte
Öffnen-Client besitzen geschützte DACLs: `SYSTEM` und lokale Administratoren erhalten Vollzugriff; `INTERACTIVE`
erhält nur die erforderlichen Durchquerungsrechte, am Blatt zusätzlich Datei-hinzufügen und am Client
Lesen/Ausführen. Die Transferdateien werden ausschließlich mit `CREATE_NEW` angelegt. Damit darf der
Originalbenutzer neue Transferdateien anlegen, aber keine vorhandenen Transferobjekte ändern oder löschen und
insbesondere kein fremdes Token lesen. Migrationsbeleg und optionaler
Tokentransfer erhalten jeweils eine exakt geprüfte, geschützte DACL für `SYSTEM`, lokale Administratoren und die
konkrete Originalbenutzer-SID. Vor dem Versiegeln prüft der erhöhte Prozess das exakte Inventar, übereinstimmende
Besitzer, jede Pfadkomponente no-follow sowie die Hardlinkfreiheit. Die Bereinigung entfernt ausschließlich die
bekannten Dateien und danach leere Verzeichnisse; sie ist bewusst nicht rekursiv.
Nicht geladene Benutzerhives werden unter gehaltenen no-follow Locks in eine administratorgeschützte
Momentaufnahme kopiert und nur von dort kurzzeitig unter einem zufälligen Namen eingebunden; jeder verbliebene
produktspezifische Autostartwert blockiert ebenso wie eine weitere Altinstallation. `NTUSER.DAT` und
`NTUSER.MAN` werden unterstützt; ein nicht eindeutig prüfbares Profil blockiert. Profil-, Registry- und
Quarantänepfade müssen auf einem festen lokalen Laufwerk liegen. Die erhöhte Inventur lehnt UNC-/Gerätepfade ab
und öffnet jede vorhandene Komponente no-follow ohne Schreib- oder Löschfreigabe, während gehaltene Handles einen
Austausch gegen Reparse-Points/Junctions bis zum Ende der Prüfung verhindern. Die Ausnahme für die zuvor
quarantänisierte EXE ist an eine nach der erhöhten Prüfung geschützte Kopie des Migrationsbelegs gebunden. Der
Originalbenutzer kann diese Kopie ausschließlich lesen, aber weder löschen, verändern noch im geschützten
Verzeichnis ersetzen. Nach Commit oder Rollback entfernt nur der erhöhte, signierte Verwaltungsclient den Beleg.
Commit und Rollback leiten den Pfad deshalb weder erneut aus dem Tempbeleg noch aus dem HKCU
des über die UAC verwendeten Administratorkontos ab. Weitere Altinstallationen werden nicht
kontenübergreifend verändert, sondern blockieren den Dienstmodus geschlossen.

#### Desktopmodus

Der Desktop-Launcher erzeugt pro Prozess ein zufälliges Browser-Sitzungstoken. Ein Startlink setzt ein
`HttpOnly`-/`SameSite=Strict`-Cookie und entfernt das Token durch Weiterleitung aus der sichtbaren URL. Weitere
Browseranfragen benötigen dieses Cookie; Host und bei schreibenden Browseranfragen der Origin werden geprüft.
Die Laufzeitdatei unter `%LOCALAPPDATA%` enthält Port, Prozess-ID und das kurzlebige Browser-Token, ist durch die
Rechte des angemeldeten Windows-Kontos geschützt und wird beim normalen Beenden beziehungsweise bei der
Deinstallation entfernt.

Ein davon getrenntes API-Token liegt dauerhaft unter
`%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt`. Bearer-Authentifizierung gilt nur für `/api/*` und gewährt
keinen Zugriff auf Startseite oder Desktop-Bootstrap. Das Token besteht ausschließlich aus URL-sicherem ASCII,
erscheint weder in URLs noch in der Laufzeitdatei und wird bei der Desktop-Deinstallation entfernt.
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
Dienstmutation erst nach einem atomar veröffentlichten, unveränderlichen `PREPARED`-Manifest, das denselben
Transaktionsbezug wie der geschützte Desktop-Seal trägt. Solange kein ebenfalls atomarer
`COMMIT_STARTED`-Marker vorliegt, darf ein Folgesetup nur die exakt belegte SCM-, Bundle- und
Maschinenzustands-Baseline restaurieren. Nach `COMMIT_STARTED` darf es ausschließlich den bereits verifizierten
Zielzustand vorwärts bereinigen. Der Folgelauf reconciliert vor seinem normalen Preflight. Fremde Dienstmetadaten,
instabile SCM-Zustände, unbekannte Bundlekombinationen, Hash-/Transaktionsabweichungen, ein anderer gebundener
Benutzer oder verwaiste nichtterminale Belege führen ohne Mutation zum geschlossenen Abbruch. Die nativen
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

SCM-Kommandos steuern Start und Stopp. Vor dem ersten Java-Kindprozess tritt der Dienst einem
Kill-on-close-Job-Objekt bei, das alle späteren Java-Prozesse bereits bei ihrer Erzeugung erben. Beim Stoppen werden
Listener und IPC geordnet geschlossen und aktive KoSIT-Prozesse nur innerhalb einer dokumentierten Grenze beendet;
ein harter Dienstabbruch schließt den Job und beendet den gesamten Kindprozessbaum. Konsolenausgabe und Prüfbericht
werden bereits beim Lesen durch feste Bytebudgets begrenzt. Die temporäre Rechnungs-XML wird exklusiv angelegt und
im `finally`-Pfad jeder kontrollierten Ausführung gelöscht. Sie verwendet unter Windows ausdrücklich kein
Delete-on-close, weil der dafür erforderliche Delete-Share-Modus den Datei-Open des Java-Prozesses verhindern kann.
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
KoSIT-Dateien bleiben unter einem neu angelegten, zufälligen Tempverzeichnis und werden nach der kontrollierten
Ausführung entfernt; im Dienstmodus liegt dieser Baum unter dem verifizierten privaten ProgramData-Verzeichnis
und ist bereits ab seiner atomaren Erstellung durch die service-spezifische DACL geschützt.

### Falsche Validierungsentscheidung

Ein Prozessfehler ohne validen VARL-Bericht ist kein Rechnungsurteil. Eine vorhandene `accept`/`reject`-Entscheidung im Bericht ist maßgeblich und wird gegen den Rückgabecode plausibilisiert.

### Geheimnisse und echte Rechnungen im Repository

`.gitignore`, Release-Filter und `AGENTS.md` schließen lokale Konfigurationen, KoSIT-/Java-Dateien, Download-Caches, PDFs, Schlüssel und nicht freigegebene XML-Dateien aus. Die Schutzwirkung ersetzt keine Review von `git status` und Release-Inhalten. Der Windows-Build nimmt ausschließlich die gesperrten Komponenten in sein eigenes Endbenutzerartefakt auf.

## Nicht abgedeckt

- Authentifizierung oder Mandantentrennung
- Malware-Scanning beliebiger PDF-Inhalte
- digitale Signaturprüfung
- Hardware-Isolation des Java-Prozesses
- Schutz gegen einen bereits kompromittierten lokalen Rechner
- rechtssichere Langzeitarchivierung
