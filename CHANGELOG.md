# Änderungsprotokoll

Alle wesentlichen Änderungen werden in diesem Dokument festgehalten. Das Projekt verwendet Semantic Versioning.

## Unveröffentlicht

Vorgesehene Version: 1.4.0.

### Windows-Dienst und Desktopbetrieb

- zusätzlicher administrativer, systemweiter Windows-Dienst-Installer mit eigener App-ID; der vorhandene nicht privilegierte Desktop-/Tray-Installer und sein optionaler HKCU-Autostart bleiben als eigenständige Betriebsart erhalten
- gemeinsamer UI-freier Loopback-Serverlebenszyklus für Desktop und Dienst; ein geschützter maschinenweiter Mutex und die feste Portreservierung verhindern parallele Backends und schlagen bei Konflikten geschlossen fehl
- SCM-kompatibler Dienst unter `LocalService` mit dienstspezifischem SID, verzögertem automatischem oder manuellem Start, Recovery-Aktionen, geordnetem Stopp und begrenztem Beenden laufender KoSIT-Prozesse; ein vor dem ersten Java-Start geerbtes Kill-on-close-Job-Objekt, feste Ausgabe-/Berichtsbudgets sowie ein unter dem verifizierten privaten ProgramData-Elternpfad atomar service-spezifisch geschützter temporärer KoSIT-Baum für Rechnungs-XML und VARL härten den Prozesspfad gegen Lesen und Namensaustausch durch andere `LocalService`-Dienste
- unveränderliche Dienstdateien unter `%ProgramFiles%` sowie streng validierte Maschinenkonfiguration, dauerhaft atomar verwaltetes API-Token und datensparsame, nach jeder Rotation erneut ACL-geschützte technische Logs unter dem per Windows-Known-Folder-API bestimmten `%ProgramData%`; IPC-Fehler protokollieren nur Phase, Exception-Typ und numerischen Windows-Fehler, niemals Anfrage, Browseradresse oder Token
- explizite geschützte DACLs für `SYSTEM`, lokale Administratoren und den Service-SID; konkrete Node-RED-Identitäten können gezielt Leserechte erhalten, breite lokale Gruppen dagegen nicht
- Dienststart und Neuinstallation tolerieren ausschließlich auf den beiden geschützten Dienstverzeichnissen genau einen von Windows Explorer erzeugten, expliziten Vollzugriffs-ACE für einen direkten Benutzer der lokalen Administratorgruppe; Dateien, Gruppen, abweichende Rechte und mehrere Zusatzidentitäten bleiben geschlossen abgewiesen, und Dienst beziehungsweise erhöhte Setup-Vorprüfung stellen vor dem Lesen des Maschinenzustands die kanonische DACL wieder her
- interaktiver Öffnen-Client mit authentifizierter lokaler Named-Pipe-IPC, bestätigter Antwortübergabe und kurzlebigem, einmaligem Browserbootstrap; das dauerhafte Bearer-Token gelangt nicht in URL, Browser-Speicher, Pipe oder normale Logs, und harte Kapazitätsgrenzen begrenzen lokale Bootstrap- und Sitzungstabellen
- kontrollierter Wechsel vom Desktop zum Dienst beendet die Tray-App, entfernt den exakten HKCU-Autostart, deaktiviert die alte Backend-EXE transaktional und übernimmt ein gültiges Desktop-Token nur nach ausdrücklicher Zustimmung und mit neuer Maschinen-DACL; laufende Altprozesse, Autostarts und weitere v1.3-Installationen werden sitzungs- beziehungsweise profilübergreifend einschließlich nicht geladener und Entra-ID-Profile erkannt und blockieren den Moduswechsel
- der Dienst-Installer aktiviert seinen Assistenten nach dem UAC-Wechsel erst nach dem sichtbaren Einblenden einmalig; verweigert Windows die Fokusübernahme, hält ein eingabefreier, auf zehn Sekunden begrenzter Sichtbarkeitshinweis das Fenster vorübergehend über dem Ausgangsfenster; ein bestätigter Abbruch auf der Lizenzseite beendet das Setup vor jeder noch nicht initialisierten Installationspfad- oder Rollbackauswertung
- ein interaktiver Direktstart der Dienst-EXE endet kontrolliert mit einem deutschen Hinweis auf den Öffnen-Client, während der normale SCM-Start und Session 0 weiterhin ohne interaktive Oberfläche arbeiten
- Migrationsplan und optionale Tokenübergabe verwenden einen kurzlebigen, DACL-geschützten Transferbaum unter `%ProgramData%` statt des privaten Inno-Tempverzeichnisses; exakte Inventur, no-follow-/Hardlink-Prüfungen und nichtrekursive Bereinigung begrenzen den Austausch auf die erwarteten Objekte
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
