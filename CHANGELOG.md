# Änderungsprotokoll

Alle wesentlichen Änderungen werden in diesem Dokument festgehalten. Das Projekt verwendet Semantic Versioning.

## Unveröffentlicht

### Wartung

- Azure-Login im Release-Workflow auf die native Node.js-24-Version aktualisiert

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
