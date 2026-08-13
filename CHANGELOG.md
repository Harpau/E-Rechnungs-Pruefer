# Änderungsprotokoll

Alle wesentlichen Änderungen werden in diesem Dokument festgehalten. Das Projekt verwendet Semantic Versioning.

## Unveröffentlicht

## 2.0.2 – 2026-08-17

### Windows-Oberfläche und KoSIT-Ausführung

- HTML, JavaScript und CSS werden über eine aus Anwendungsversion, Analyseschema und Assetinhalt berechnete
  UI-Revision ausgeliefert. Die Startseite ist nicht cachebar, statische Dateien besitzen inhaltsadressierte
  Pfade, und veraltete offene Browser-Tabs werden kontrolliert mit `409 ui_version_mismatch` oder nach einem
  Prozessneustart mit `403 desktop_session_error` samt Wiederöffnungshinweis beendet. Damit
  kann nach einem Update kein gemischter HTML-/JavaScript-Stand mehr den bisherigen Fehler
  `Cannot set properties of null` bei einer KoSIT-Prüfung auslösen.
- Desktop- und Dienst-Pakettests prüfen den Revisionsvertrag, die Cache-Header, einen veralteten Browseraufruf
  und weiterhin einen davon unabhängigen Bearer-API-Aufruf. Die manuelle Abnahme ist risikobasiert reduziert:
  Windows 10 22H2 erhält einen Best-Effort-Neuinstallations-/Lauffähigkeits-Smoke einschließlich des langen
  XSD-Zielpfads; unter Windows 11 verbleiben die fokussierten Desktop- und Dienst-Upgrades von 2.0.1. Historische
  Upgrades, Reboot-/Recovery-, Identitäts- und Performanceprüfungen sind nur noch anlassbezogen verpflichtend.
- Der schlanke Öffnen-Client des Windows-Dienstpakets lädt beim Start keine Browser-Assets mehr. Fehler vor oder
  während seiner internen Ausführung enden kontrolliert und ohne modalen PyInstaller-Dialog, sodass ein
  unbeaufsichtigter Installer nicht auf eine unsichtbare Fehlerbestätigung wartet.

### Prüfung und Analyseschema 2

- Vorhandene, aber unparsebare Pflichtwerte wie ein ungültiges Rechnungsdatum oder `NaN` als Zahlbetrag führen
  nun zu expliziten Formatfehlern. Sie können beim Aufbau des öffentlichen Modells nicht mehr als `null`
  verschwinden und gleichzeitig den internen Status `clear` erzeugen.
- Befundvorkommen trennen den nullbasierten Arrayindex einer Rechnungsposition von ihrer fachlichen Kennung.
  Eine Position mit der Kennung `42` verweist daher korrekt auf `/lines/0` und behält `42` separat als
  `identifier`.
- Rechnungswerte oberhalb der dokumentierten Feldlängen führen kontrolliert und ohne Rückgabe des Werts zu
  HTTP 422 `invoice_input_error`; andere interne Modellfehler bleiben weiterhin sichtbar und werden nicht
  pauschal als Eingabefehler maskiert.
- Große Dezimalwerte innerhalb des begrenzten Verarbeitungsbudgets bleiben bei Berechnungen sowie in API-,
  HTML- und PDF-Ausgaben exakt. Finanzielle Operanden mit mehr als 4.096 Ziffern werden kontrolliert und ohne
  Rückgabe des Werts mit HTTP 422 abgewiesen, statt einen internen Fehler auszulösen.

### XML-Sicherheit und Parser

- Ein vorgeschaltetes XML-Strukturbudget begrenzt Elemente, Attribute, Namespace-Deklarationen, Kommentare und
  Processing Instructions vor dem Aufbau des eigentlichen XML-Baums. Die technische Feldliste verwendet
  lineare Geschwisterzähler sowie ein eigenes monotones Zeitbudget und weist Zeilen- und Zeitbegrenzungen im
  Verarbeitungsstatus getrennt aus.
- UBL- und CII-Parser wählen Fachfelder ausschließlich über die vorgeschriebenen Namespace-URIs aus. Korrekte,
  frei gewählte Präfixe bleiben zulässig; gleichnamige Elemente aus fremden Child-Namespaces werden nicht mehr
  als Rechnungsdaten interpretiert und bleiben im technischen Anhang erhalten.
- Direkte Textwerte bleiben auch dann erhalten, wenn XML-Kommentare oder Processing Instructions den Text
  aufteilen. Die zulässigen direkten CII-Varianten für Datums- und Indikatorwerte werden weiterhin erkannt,
  ohne dabei gleichnamige Inhalte aus fremden Wrapper-Namespaces zu übernehmen.

### Abhängigkeiten, Automatisierung und Releaseprozess

- `pypdf` ist in Laufzeitdeklarationen und Windows-Release-Lock mindestens beziehungsweise exakt auf 6.15.0
  aktualisiert. Damit enthält das neue Windows-Paket die Korrekturen für CVE-2026-71852 und CVE-2026-71870.
- Der Node-RED-Mailflow behandelt `official=unsupported` auch bei verpflichtender offizieller Prüfung als
  qualifizierten Bericht und verarbeitet anschließend weitere Kandidaten. Der Status wird weder als offizielle
  Annahme noch als offizielle Ablehnung ausgegeben; die Behandlung von `not-requested`, `unavailable` und
  `indeterminate` bleibt unverändert. Bereits importierte Flow-Kopien müssen manuell aktualisiert oder neu
  importiert werden.
- Signierte Vorab-Artefakte werden 14 Tage aufbewahrt. Ein Tag-Lauf erzeugt nach allen Prüfungen ausschließlich
  einen GitHub-Release-Draft; die exakt taggebauten und signierten Dateien müssen vor einer manuellen
  Veröffentlichung erneut geprüft und protokolliert werden.
- Windows-Installer werden ausschließlich mit dem festgeschriebenen Inno Setup 7.0.2 x64 gebaut. Dadurch können
  auch enthaltene XSD-Dateien installiert werden, deren vollständiger Zielpfad die klassische 260-Zeichen-Grenze
  erreicht; ein abweichender oder ungeprüfter Installercompiler wird vom Build abgewiesen.

## 2.0.1 – 2026-08-06

### Browseroberfläche

- Der Ladezustand kombiniert den animierten Fortschrittsindikator nicht mehr mit einer großflächigen
  Hintergrundunschärfe. Dadurch sättigt Microsoft Edge bei offiziellen CII-/UBL-Prüfungen über RDP auf
  ressourcenarmen Windows-VMs nicht mehr dauerhaft die CPU. KoSIT-Ausführung und Standardtimeout von 60 Sekunden
  bleiben unverändert.

## 2.0.0 – 2026-08-03

### Breaking API-Änderung

- `POST /api/analyze` liefert ab sofort ausschließlich das geschlossene Analyseschema 2 am bestehenden
  Endpunkt. Es gibt keinen Legacy-Endpunkt, Versionsparameter oder Adapter für das bisherige Schema; Consumer
  müssen `schema_version == 2` verlangen und gemeinsam mit dem Server migriert werden.
- Der bisherige Sammelblock `validation` wurde durch die drei unabhängigen Achsen `assessment.official`,
  `assessment.internal` und `assessment.processing` ersetzt. Ein gemeinsamer
  `ok`-/`warning`-/`invalid`-Status wird nicht mehr gebildet.
- HTML- und PDF-Berichte liefern jetzt `X-Einvoice-Analysis-Schema`, `X-Einvoice-Syntax`,
  `X-Einvoice-Conformity-Status`, `X-Einvoice-Internal-Status` und `X-Einvoice-Processing-Status`. Die früheren
  Header `X-Einvoice-Validation-Status` und `X-Einvoice-Official-Status` entfallen.
- HTML- und PDF-Berichte verwenden den Form-Parameter `scope=readable|complete` und standardmäßig den
  menschenlesbaren Umfang `readable`; der tatsächlich gelieferte Umfang steht in
  `X-Einvoice-Report-Scope`. Technische XML-/KoSIT-Rohdaten sind nur noch mit `scope=complete` enthalten.
- Beträge, Mengen, Codes, Kennungen, Parteien, Positionen, Steuern, Zahlungsanweisungen, Quellmetadaten und
  technische Felder wurden in explizite Schema-2-Objekte überführt. Die Migrationsanleitung mit den zentralen
  Alt→Neu-Zuordnungen steht in `docs/API_MIGRATION_V2.md`.
- Browseroberfläche, Windows-Pakettests und der mitgelieferte Node-RED-Flow wurden atomar auf Schema 2 umgestellt.
  Bereits importierte Flow-Kopien und eigene Consumer müssen gleichzeitig aktualisiert werden; der neue Flow
  verlangt Schemaversion 2, alle sechs Berichtsheader und ausdrücklich `scope=readable`.

### Dokumenttypen, Rollen und Prüfung

- versionierte Registry für alle 62 Dokumenttypcodes der gebündelten CEN-EN-16931-Regeln 1.3.15 ergänzt;
  bekannte, unbekannte und fehlende Codes bleiben unterscheidbar, und UBL `Invoice`/`CreditNote` wird auf
  Root-Kompatibilität geprüft
- Profil- und Self-Billing-Semantik trennt Dokumentaussteller/-empfänger von Gläubiger/Schuldner und einer aus
  Typ und Vorzeichen abgeleiteten erwarteten Zahlungsrichtung; diese Rollen behaupten keine tatsächliche oder
  zwingende Zahlung
- insbesondere bleibt bei einer positiven Eigenabrechnung des Typs `389` die erwartete Zahlungsrichtung
  Käufer → Verkäufer, während sie bei einer positiven Gutschrift des Typs `381` Verkäufer → Käufer lautet
- Zahlungsprüfungen auf die konkreten Regeln BR-CO-25, BR-49, XRechnung BR-DE-1 und BR-DE-17 ausgerichtet; ein
  positiver Zahlbetrag allein erzeugt keine allgemeine Pflicht zu einer Zahlungsanweisung
- Befunde enthalten strukturierte Regelmetadaten, fachliche `semantic_references`, ein separates
  `occurrence` und nur bei realer XML-Fundstelle `xml_location`; BG-/BT-Kennungen werden nicht mehr als
  technische Ortsangaben dargestellt
- XML-Syntax und Decimal-Werte werden strikt verarbeitet; nichtendliche Werte, Exponenten, Dezimalkomma und
  freie Texte werden nicht stillschweigend in Rechenwerte umgewandelt

### Berichts- und Druckdarstellung

- Browseransicht, eigenständiger HTML-Bericht und ReportLab-PDF verwenden einen gemeinsamen
  Präsentationsvertrag für deutsche Statusbezeichnungen, 30 Kopffakten, Zahlungsfluss und Abschnittsreihenfolge
- „Drucken / PDF“ und „HTML-Bericht“ erzeugen den lesbaren Bericht; die getrennte Aktion „Vollständiger Bericht“
  lädt den eigenständigen HTML-Bericht einschließlich der technischen Anhänge
- der direkte Browserdruck enthält Rechnungsdarstellung und menschenlesbare Prüfergebnisse, blendet jedoch
  Roh-XML und technische Detailansichten aus; der Druckkopf ist hell und tintensparend
- In Rechnungspositionen steht der Steuersatz nun im Vordergrund und der Kategoriecode platzsparend darunter;
  ein zusätzlicher Hinweis erscheint nur bei Abweichungen zwischen Positionen und Steueraufschlüsselung. Der
  Steuersatz beziehungsweise ersatzweise Kategoriecode verwendet in Browser- und Druckdarstellung dieselbe
  Schriftgröße und Fettschrift wie der Nettobetrag; sekundäre Steuerangaben bleiben bewusst kleiner.

### Abdeckung, Datenschutz und Komponenten

- CII-/UBL-Feldabdeckung für Parteien, Rollen, Zeiträume, Referenzen, Positionsdetails, Preisbestandteile,
  Nachlässe/Zuschläge, Steuerwährungen und Zahlungsarten erweitert; unbekannte XML-Inhalte bleiben im
  technischen Anhang und im bytegetreuen Export verfügbar
- Liefertermin, Lieferort, Lieferempfänger und Liefer-/Leistungszeitraum werden getrennt modelliert; direkte
  Parteienkennungen bleiben von rechtlichen Registerkennungen unterscheidbar, UBL-Steuerzeitpunktcodes (BT-8)
  werden nicht als Zeitraumtexte ausgegeben und Hinweisartcodes (BT-21) bleiben strukturiert erhalten
- Kartenkontokennungen werden im strukturierten Modell maskiert und aus technischen XML-Textansichten redigiert;
  der ausdrücklich bytegetreue `/api/xml`-Export bleibt unverändert und ist kein anonymisierter Export
- der interaktive Quellstart richtet bei konfiguriertem API-Bearer-Token automatisch eine getrennte,
  cookiegeschützte Browsersitzung ein, sodass Oberfläche und Beispiele funktionieren, ohne das dauerhafte
  Automatisierungstoken an den Browser weiterzugeben
- öffentliche Health-Metadaten und Dokumentation weisen die gepinnten Komponenten aus
  `packaging/kosit/components.lock.json` aus: KoSIT Validator 1.6.2, XRechnung 3.0.2,
  Konfigurationsstand 2026-01-31, CEN EN 16931 1.3.15 und XRechnung-Schematron 2.5.0
- der KoSIT-Installer ermittelt keine wechselnde neueste Veröffentlichung mehr, sondern installiert nur die in
  dieser Sperrdatei festgelegten Artefakte nach verpflichtender SHA-256-Prüfung
- Die Browserübersicht zeigt zu Beginn nur noch 30 wesentliche Rechnungsfelder; die Typregister-Version bleibt
  im technischen Schema verfügbar. Die Kopfzeile nennt die Rechnungsart mit Dokumenttypcode und Bezeichnung und
  kennzeichnet unbekannte oder fehlende Codes ausdrücklich. Das Gesamtstatusfeld bleibt mit allen möglichen
  Beschriftungen einzeilig und vollständig; die Kopfzeile wechselt anhand ihrer tatsächlich verfügbaren Breite
  kontrolliert in die zweizeilige Anordnung. Im Zahlungsbereich ersetzen Dokumentfluss und erwarteter
  Zahlungsfluss die ausführliche Rollenmatrix. Semantische Überschriftenebenen für Zahlungsabschnitte,
  -anweisungen und -details sowie deutlich kleinere Ableitungshinweise und zum Folgeinhalt gesetzte Abstände
  verbessern die visuelle Gruppierung.

### Release-Paketierung und Dokumentation

- Der gemeinsame Präsentationsvertrag wird in Wheel, Source Distribution und beiden Windows-Laufzeitpaketen
  mitgeführt. Die Source Distribution enthält außerdem die zentrale KoSIT-Sperrdatei, den Node-RED-Beispielflow
  und die zugehörigen JavaScript-Regressionstests.
- Das Repository-Release-ZIP schließt auch verschachtelte `.env`-Varianten aus; allein die Vorlage
  `.env.example` bleibt zulässig. Der Versionsabgleich prüft zusätzlich den Versionskopf in `START_HERE.txt` und
  genau einen datierten Changelog-Abschnitt für die aktuelle Version.
- Die GitHub-Release-Notizen stellen den inkompatiblen Wechsel auf Analyseschema 2 vor die automatisch erzeugten
  Notizen und verlinken die Migrationsanleitung sowie das kuratierte Changelog.

## 1.5.0 – 2026-07-28

### Windows-Dienst und Desktopbetrieb

- Der Dienst-Installer führt keinen automatischen Wechsel aus einer vorhandenen Desktopinstallation mehr durch. Der Desktopmodus muss einschließlich Autostart vor der Dienstinstallation vollständig entfernt werden; Desktop und Dienst bleiben getrennte, nicht parallel zu betreibende Betriebsarten.
- Die bisherige Inno-Option `/MIGRATEDESKTOPTOKEN=1` wird ersatzlos nicht mehr unterstützt. Der Dienst erzeugt beziehungsweise erhält sein eigenes Maschinentoken; Integrationen wie Node-RED müssen dieses Token kontrolliert neu provisionieren.
- Bei der Dienstdeinstallation ausdrücklich erhaltenes ProgramData gilt allein nicht als installierter Gegenmodus: Der Desktopmodus darf es weder blockieren noch verändern, und eine spätere Dienstneuinstallation verwendet dasselbe geschützte Maschinentoken weiter.
- Unvollständige v1.4.0-Migrations-, Transfer-, Seal-, Quarantäne- und kombinierte Alttransaktionszustände werden nicht übernommen oder automatisch wiederhergestellt. Paket- und Freigabetests müssen deshalb auf einer sauberen Wegwerf-VM ohne solche Altzustände beginnen.
- Der interne Dienst-Testinstaller sowie seine Build- und Testparameter sind recovery-neutral benannt. Der service-only `COMMIT_STARTED`-Hard-Kill-Test, Rollback/Roll-forward und die manuelle Reboot-Abnahme bleiben erhalten; der v1.3.0-Desktopmigrationstest entfällt.
- Die Dienst-Recovery unterscheidet bei einem verbliebenen `COMMIT_STARTED`-Beleg zwischen noch ausstehenden Vorwärtsaktionen und einem bereits vollständig erreichten, stabil geprüften Zielzustand; im terminalen Fall finalisiert sie nur noch den geschützten Transaktionsbeleg, statt erneut einen Roll-forward zu melden.
- Der Dienst-Preflight öffnet abgemeldete `NTUSER.DAT`-/`NTUSER.MAN`-Hives nicht mehr mit dem für normale Benutzerhives ungeeigneten `RegLoadAppKeyW`. Stattdessen liest ein zeitbegrenzter Hilfsprozess genau einen gesperrten, größenbegrenzten Snapshot rein lesend in den gepinnten Offline-Parser Regipy ein, ohne den Hive zu mounten oder eine Kopie auf Datenträger anzulegen.
- Dadurch werden auch nicht laufende Desktop-Altinstallationen und Autostarts in benutzerdefinierten Verzeichnissen abgemeldeter Profile weiterhin vor jeder Maschinenänderung erkannt. Inkonsistente, veränderte, umgeleitete, mehrdeutige oder nicht vollständig auswertbare Hives blockieren die Dienstinstallation geschlossen.

## 1.4.0 – 2026-07-26

### Windows-Dienst und Desktopbetrieb

- zusätzlicher administrativer, systemweiter Windows-Dienst-Installer mit eigener App-ID; der vorhandene nicht privilegierte Desktop-/Tray-Installer und sein optionaler HKCU-Autostart bleiben als eigenständige Betriebsart erhalten
- gemeinsamer UI-freier Loopback-Serverlebenszyklus für Desktop und Dienst; ein geschützter maschinenweiter Mutex und die feste Portreservierung verhindern parallele Backends und schlagen bei Konflikten geschlossen fehl
- SCM-kompatibler Dienst unter `LocalService` mit dienstspezifischem SID, verzögertem automatischem oder manuellem Start, Recovery-Aktionen, geordnetem Stopp und begrenztem Beenden laufender KoSIT-Prozesse; ein vor dem ersten Java-Start geerbtes Kill-on-close-Job-Objekt, feste Ausgabe-/Berichtsbudgets sowie ein unter dem verifizierten privaten ProgramData-Elternpfad atomar service-spezifisch geschützter temporärer KoSIT-Baum für Rechnungs-XML und VARL härten den Prozesspfad gegen Lesen und Namensaustausch durch andere `LocalService`-Dienste
- unveränderliche Dienstdateien unter `%ProgramFiles%` sowie streng validierte Maschinenkonfiguration, dauerhaft atomar verwaltetes API-Token und datensparsame, nach jeder Rotation erneut ACL-geschützte technische Logs unter dem per Windows-Known-Folder-API bestimmten `%ProgramData%`; IPC-Fehler protokollieren nur Phase, Exception-Typ und numerischen Windows-Fehler, niemals Anfrage, Browseradresse oder Token
- explizite geschützte DACLs für `SYSTEM`, lokale Administratoren und den Service-SID; konkrete Node-RED-Identitäten können gezielt Leserechte erhalten, breite lokale Gruppen dagegen nicht
- Dienststart und Neuinstallation tolerieren ausschließlich auf den beiden geschützten Dienstverzeichnissen genau einen von Windows Explorer erzeugten, expliziten Vollzugriffs-ACE für einen direkten Benutzer der lokalen Administratorgruppe; Dateien, Gruppen, abweichende Rechte und mehrere Zusatzidentitäten bleiben geschlossen abgewiesen, und Dienst beziehungsweise erhöhte Setup-Vorprüfung stellen vor dem Lesen des Maschinenzustands die kanonische DACL wieder her
- interaktiver Öffnen-Client mit authentifizierter lokaler Named-Pipe-IPC, bestätigter Antwortübergabe und kurzlebigem, einmaligem Browserbootstrap; das dauerhafte Bearer-Token gelangt nicht in URL, Browser-Speicher, Pipe oder normale Logs, und harte Kapazitätsgrenzen begrenzen lokale Bootstrap- und Sitzungstabellen
- kontrollierter Wechsel vom Desktop zum Dienst beendet die Tray-App, entfernt den exakten HKCU-Autostart, deaktiviert die alte Backend-EXE transaktional und übernimmt ein gültiges Desktop-Token nur nach ausdrücklicher Zustimmung und mit neuer Maschinen-DACL; ein fehlgeschlagener Wechsel startet den zuvor laufenden Desktop aus dem ursprünglichen Programmverzeichnis und ohne geerbten PyInstaller-Zustand wieder; laufende Altprozesse, Autostarts und weitere v1.3-Installationen werden sitzungs- beziehungsweise profilübergreifend einschließlich nicht geladener und Entra-ID-Profile erkannt und blockieren den Moduswechsel
- der Dienst-Installer aktiviert seinen Assistenten nach dem UAC-Wechsel erst nach dem sichtbaren Einblenden einmalig; verweigert Windows die Fokusübernahme, hält ein eingabefreier, auf zehn Sekunden begrenzter Sichtbarkeitshinweis das Fenster vorübergehend über dem Ausgangsfenster; ein bestätigter Abbruch auf der Lizenzseite beendet das Setup vor jeder noch nicht initialisierten Installationspfad- oder Rollbackauswertung
- ein interaktiver Direktstart der Dienst-EXE endet kontrolliert mit einem deutschen Hinweis auf den Öffnen-Client, während der normale SCM-Start und Session 0 weiterhin ohne interaktive Oberfläche arbeiten
- Migrationsplan und optionale Tokenübergabe verwenden einen kurzlebigen, DACL-geschützten Transferbaum unter `%ProgramData%` statt des privaten Inno-Tempverzeichnisses; exakte Inventur, no-follow-/Hardlink-Prüfungen und nichtrekursive Bereinigung begrenzen den Austausch auf die erwarteten Objekte, und ein Folgelauf entfernt ausschließlich streng erkennbare Transferreste eines zuvor unterbrochenen Setups
- Dienstupdates stoppen und deaktivieren den Dienst vor dem atomaren Austausch des vollständigen Bundlebaums, erhalten Konfiguration und Token, sichern SCM-Metadaten über die Dienst-APIs und starten nur einen zuvor laufenden Dienst neu; ein gemeinsamer systemweiter Vorgangsmutex serialisiert Setup, Update, Recovery und Deinstallation sitzungsübergreifend; die Deinstallation ist über einen getrennten atomaren SCM-/RUNNING-Beleg wiederaufnehmbar, entfernt den SCM-Dienst und löscht ProgramData nur nach klarer Benutzerentscheidung sowie erneuter, geschlossener Prüfung jedes bekannten Zustandsobjekts; exakt inventarisierte transiente KoSIT-Crashreste werden dagegen beim nächsten Dienststart und bei jeder Deinstallation unabhängig von der Aufbewahrungsentscheidung entfernt
- Besitzer-, Reparse-Point-/Junction- und Hardlink-Prüfungen härten den Maschinenzustand vor Lesen, Schreiben und ACL-Änderungen; Log-DACLs unterdrücken die implizite Schreibberechtigung des dienstübergreifend geteilten `LocalService`-Besitzers, die erhöhte Profilinventur hält ausschließlich lokale Pfadkomponenten no-follow ohne Schreib-/Löschfreigabe und lädt Offline-Hives nur aus einer administrativen Momentaufnahme, und ein geschützter Phasenbeleg bindet Preflight, Commit und Rollback; unsichere Altzustände werden nicht durch Neuvergabe von ACLs übernommen

### API und Automatisierung

- lokaler Node-RED-Vertrag für Endpunkt, Bearer-Authentifizierung, Status, Retry und Fehler bleibt unverändert in Desktop- und Dienstmodus verfügbar
- `EINVOICE_REQUIRE_KOSIT=false` sendet weiterhin `official=false` und überspringt KoSIT tatsächlich; PDF-Bericht, KoSIT-Seitenumbruch und bytegetreuer Original-XML-Export bleiben regressionsgeprüft
- sichere Diensttoken-Provisionierung und -rotation für eine zuvor ermittelte Node-RED-Windows-Identität dokumentiert; für einen vollständigen Ablauf vor Benutzeranmeldung muss auch Node-RED als Dienst laufen

### Windows-Build und Qualität

- getrennte PyInstaller-Artefakte für Desktop, Dienst und Öffnen-Client sowie zwei eindeutig benannte Inno-Setup-Installer ergänzt
- Authenticode-Signierung und nachgelagerte SHA-256-Prüfsummen decken alle drei eigenen EXEs und beide Installer ab; ein veröffentlichtes Bundle-ZIP macht die im Manifest genannten EXE-Pfade prüfbar
- Windows-Integrationstests prüfen Desktop- und Dienstinstallation, API/PDF/XML, reale KoSIT-Ausführung, SCM/DACLs, Tokenpersistenz, laufende Updates, Deinstallation sowie die Migration vom veröffentlichten Desktopstand v1.3.0
- der Windows-Dienstpakettest bestimmt direkte Benutzer und das tatsächlich verwendete Dienstkonto ausschließlich über sprachneutrale numerische Kontotypen und SIDs, sodass deutsche und englische Windows-Installationen denselben Sicherheitsvertrag prüfen
- manuelle signierte Vorab-Probeläufe stellen den internen Recovery-Testinstaller für einen Tag als getrenntes, deutlich markiertes Actions-Artefakt bereit; Tag-Läufe, Produktionsartefakte und öffentliche Releases schließen ihn weiterhin aus
- Releaseprozess verlangt weiterhin einen signierten Vorab-Probelauf, eine manuelle Windows-11-Abnahme einschließlich echtem Neustart und Dienststart vor Anmeldung sowie ausdrückliche Freigabe vor Tag und öffentlicher Veröffentlichung

## 1.3.0 – 2026-07-22

### API und Automatisierung

- HTML-Berichte liefern maschinenlesbare Header für erkannte Syntax, gemeinsamen Prüfstatus und den differenzierten KoSIT-Status
- zusätzlicher PDF-Berichtsendpunkt mit festen, datensparsamen Antwortnamen; der Node-RED-Mailflow versendet direkt öffnungsfähige PDF- statt temporärer HTML-Anhänge
- robuste PDF-Darstellung mit eingebetteten Noto-Schriften, sichtbarem Fallback für nicht unterstützte Zeichen, festen Inhaltsbudgets, 200-Seiten-Schutz und begrenzter Render-Parallelität
- technische KoSIT-Rohberichte beginnen mit ihrer Überschrift auf einer neuen Seite und nutzen den verfügbaren Seitenraum ohne unteilbare Textblöcke
- Rechnungsanalysen und KoSIT-Aufrufe blockieren den API-Event-Loop nicht mehr, sind pro Prozess auf zwei gleichzeitige Prüfungen begrenzt und melden Überlast sofort mit `503`/`Retry-After`
- installierte Windows-App stellt `/api/*` auf einem festen Loopback-Port mit einem separaten persistenten Bearer-Token für lokale Automatisierungen bereit
- API-Token-Schutz greift auch ohne Desktop-Sitzung; der öffentliche Healthcheck prüft weiterhin den lokalen Host und veröffentlicht nur Version und KoSIT-Bereitschaft
- Windows-Launcher unterstützt mit `--background` einen stillen Start von Webserver und Infobereich ohne automatisches Browserfenster
- Windows-Installer bietet einen optionalen, nicht privilegierten Autostart bei Benutzeranmeldung und entfernt ihn bei Abwahl oder Deinstallation
- Installer und Uninstaller können eine laufende neue Desktop-Version kontrolliert beenden; ein laufender Autostart wird nach einem Update im Hintergrund wiederhergestellt
- API-Token werden als URL-sicheres ASCII validiert und auch frühe Port-/Konfigurationsfehler im Startprotokoll festgehalten
- anonymisierter Node-RED-Beispielflow enthält einen sicher vorkonfigurierten IMAP-Eingang, verarbeitet alle XML-/PDF-Kandidaten über die lokale Berichts-API, trennt Verbindungsfehler vom normalen Antwortpfad und quittiert erst nach terminalem Abschluss
- die lokale API-URL wird mit einer Node-RED-Function-kompatiblen, streng verankerten Prüfung validiert; der Test-Harness bildet die eingeschränkte Node-RED-Sandbox nach
- `EINVOICE_REQUIRE_KOSIT=false` wird vom Node-RED-Flow als `official=false` an die Berichts-API weitergegeben und überspringt die KoSIT-Prüfung tatsächlich
- lokale Healthchecks und der Node-RED-Berichtsaufruf umgehen Prozess-Proxys ausdrücklich, damit weder lokale Starts fehlschlagen noch Rechnungsdaten oder API-Token an externe Proxys gelangen
- echter Node.js-Laufzeittest prüft Multipart-Bytes, Status-/Retryregeln, Mehrfachberichte und SMTP-/IMAP-ACK-Semantik; das HTTP-Zeitlimit wird wirksam über `msg.requestTimeout` gesetzt

### Dokumentation

- verbindlichen fachlichen Vertrag für Node-RED- und andere Automatisierungsintegrationen mit getrennten Erkennungs-, Prüf- und KoSIT-Status, Fehlerklassen, Retry- und Quittierungsregeln ergänzt

### Wartung

- Azure-Login im Release-Workflow auf die native Node.js-24-Version aktualisiert
- Windows-Pakettest verweigert Eingriffe in bestehende Installationen und Benutzerzustände, bereinigt nur den eigenen Testprozess und verlangt eine ausdrücklich bestätigte Wegwerf-VM oder Testidentität
- optionale Autostart-Registrywerte werden im Windows-Pakettest auch auf vollständig sauberen Benutzerprofilen kontrolliert und ohne irreführenden Vorabfehler gelesen

## 1.2.0 – 2026-07-20

### Windows-Paket

- nativer Windows-x64-Installer mit eingebettetem Python, Java, festgeschriebenem KoSIT-Validator und XRechnung-Konfiguration vorbereitet
- Desktop-Launcher mit dynamischem Loopback-Port, Einmal-Startlink, strengem Sitzungscookie, Host-/Origin-Prüfung, Einzelinstanz und Infobereich-Menü ergänzt
- KoSIT-Prüfungen starten den eingebetteten Java-Prozess ohne sichtbares Terminalfenster
- Windows-Build prüft Komponenten-Hashes, Authenticode-Signaturen, Installation, echte KoSIT-Ausführung, bytegetreuen XML-Export und Deinstallation
- Release-Signierung über GitHub OIDC und einen nicht exportierbaren Azure-Key-Vault-HSM-Schlüssel ergänzt; PFX-Dateien und dauerhafte Azure-Client-Secrets sind nicht erforderlich

### Darstellung und Prüfung

- Der Browser fordert bei nicht eingerichteter KoSIT-Anbindung keine offizielle Prüfung mehr an; der deaktivierte Schalter ist nicht ausgewählt und verursacht keine irreführende Konfigurationswarnung im Prüfergebnis
- Hybrid-PDFs mit Kennwortschutz, mehrdeutigen Rechnungskandidaten, beschädigten Anhängen oder überschrittenem Dekodierungsbudget werden kontrolliert abgelehnt; leer entschlüsselbare PDFs bleiben unterstützt
- Der Konsolenstart erzeugt mit `--open` auch für explizite IPv6-Adressen eine gültige, geklammerte Browser-URL

### Qualität

- HTTPX2 als bevorzugtes TestClient-Backend ergänzt und die veraltete HTTPX-Kompatibilität durch eine gezielte Pytest-Warnungsprüfung abgesichert
- PDF-Randfälle, bytegetreuer XML-Export und Größenbegrenzungen werden durch zusätzliche Regressionstests und den Windows-Smoke-Test abgedeckt
- pypdf 6 als Mindestversion festgelegt, um dessen zusätzliche Dekompressionsbegrenzung zu nutzen
- Risikobasierte Regressionstests sichern UBL-Gutschriften, gemeinsame Parser- und XML-Hilfen, die Umgebungskonfiguration sowie den Konsolenstart ab; das kombinierte Coverage-Gate wurde auf 80 Prozent angehoben
- Java-, KoSIT- und XRechnung-Versionen für den Windows-Build werden in einer Sperrdatei mit offiziellen SHA-256-Prüfsummen nachvollziehbar festgelegt

## 1.1.0 – 2026-07-18

### Darstellung und Prüfung

- Steuergruppen zeigen Code, Bezeichnung, Steuersatz, Kategorienettobetrag beziehungsweise Bemessungsgrundlage, Begründung und Begründungscode gleichzeitig an
- Kategorie `O` wird als „Nicht der Umsatzsteuer unterliegend“ dargestellt und ohne künstliche `0 %`-Anzeige behandelt
- interne Regeln für unzulässige Steuersätze bei `O`, erforderliche Nullsätze bei `Z`, `E`, `AE`, `G` und `K` sowie Null-Steuerbeträge ergänzt
- Warnung bei semantisch widersprüchlichen Kombinationen, insbesondere `G` zusammen mit „nicht im Inland steuerbar“ oder Reverse-Charge-Hinweisen
- Konsistenzregeln für die exklusive Verwendung der Kategorie `O` ergänzt

### Codex und GitHub

- repository-weite Codex-Anweisungen in `AGENTS.md`
- vollständige Entwicklungs-, Architektur-, Validierungs-, Steuer-, Sicherheits-, GitHub- und Release-Dokumentation
- GitHub Actions für Linux-/Windows-CI, CodeQL, Dependency Audit und tagbasierte Releases
- Dependabot, Issue Forms und Pull-Request-Vorlage
- Bootstrap-, Check-, Git-Initialisierungs-, Versions- und Release-Skripte für Windows und Unix
- bereinigtes Release-ZIP mit Schutz vor versehentlich aufgenommenen Rechnungen, Schlüsseln, lokalen Konfigurationen und KoSIT-Dateien

### Qualität

- Ruff, Mypy, Pytest Coverage, Pre-commit, Build, Twine und pip-audit als Entwicklungswerkzeuge integriert
- Versionskonsistenz zwischen `VERSION`, Paketmetadaten, Anwendung und KoSIT-Installer wird automatisiert geprüft
- zusätzliche Regressionstests für Steuerdarstellung und Steuerkategorien

## 1.0.2 – 2026-07-15

- KoSIT-Berichte werden primär aus der erzeugten XML-Berichtsdatei gelesen; `-p/--print` wird nicht mehr verwendet
- KoSIT-Ausgaben der Form `[Format error!] <<?xml ...` werden als Konsolen-Darstellungsfehler erkannt
- gültige VARL-Berichte werden ersatzweise aus `stdout` oder `stderr` extrahiert
- `<rep:accept/>` beziehungsweise `<rep:reject/>` hat Vorrang vor dem Prozessrückgabecode

## 1.0.1 – 2026-07-15

- KoSIT-Installer lädt ausschließlich das ausführbare `validator-<Version>-standalone.jar`
- JAR-Manifestprüfung auf `Main-Class` und optionale SHA-256-Prüfung ergänzt
- technische Startfehler werden nicht mehr als Rechnungsablehnung dargestellt

## 1.0.0 – 2026-07-15

- erste vollständige Version mit CII-/UBL-Parsern, Hybrid-PDF-Extraktion, Webansicht, technischem XML-Anhang, interner Prüfung, optionaler KoSIT-Anbindung und Exporten
