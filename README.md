# E‑Rechnungs‑Viewer & Prüfer

Lokale Webanwendung zum Öffnen, verständlichen Darstellen und Prüfen strukturierter E‑Rechnungen. Sie liest XML-Rechnungen direkt oder extrahiert die Rechnungs-XML aus einer Hybrid-PDF. Neben einer lesbaren Rechnungsansicht entstehen ein Prüfbericht mit getrennten Bewertungsachsen, XML-Textansichten und ein navigierbarer technischer Tabellenanhang mit erfassten Elementwerten, Attributen, Namespace-URIs und Pfaden.

Die Anwendung ist als nachvollziehbares Prüf- und Analysewerkzeug konzipiert. Die eingebaute Prüfung ersetzt weder eine fachliche Steuerberatung noch eine vollständige Profilvalidierung. Für XSD-/Schematron-Prüfungen kann der offizielle KoSIT-Validator angebunden werden.

> [!IMPORTANT]
> Seit Version **2.0.0** verwendet die Analyse-API ausschließlich **Analyseschema 2**. `POST /api/analyze` wurde am
> bestehenden Endpunkt inkompatibel umgestellt; es gibt keinen Legacy-Endpunkt, Versionsparameter oder
> Kompatibilitätsadapter für Schema 1. Auch die Statusheader der HTML- und PDF-Berichte wurden ersetzt.
> Integrationen müssen Server und Consumer gemeinsam umstellen und `schema_version == 2` beziehungsweise
> `X-Einvoice-Analysis-Schema: 2` verlangen. Die Migrationsanleitung mit den zentralen Feldzuordnungen steht in
> [`docs/API_MIGRATION_V2.md`](docs/API_MIGRATION_V2.md).

## Unterstützte Eingaben

- CII / UN/CEFACT CrossIndustryInvoice D16B, darunter EN 16931 und XRechnung
- ZUGFeRD- und Factur-X-PDFs mit eingebetteter Rechnungs-XML
- UBL 2.1 `Invoice` und `CreditNote`, darunter EN-16931-, Peppol- und XRechnung-Profile
- XML in UTF-8 und UTF-16

Reine Sicht- oder Scan-PDFs ohne eingebettete strukturierte XML werden bewusst nicht per OCR rekonstruiert.

## Wichtige Funktionen

- lesbare Darstellung von Kopf, Parteien, Positionen, Einheiten, Preisbasismengen, Steuern, Summen, Zahlung, Referenzen, Lieferung und Hinweisen
- XML-Text- und Tabellenansichten sowie bytegetreuer Export der ursprünglichen XML-Bytes
- interne Pflichtfeld-, Datums-, Format-, Rechen- und Plausibilitätsprüfungen
- optionale KoSIT-Prüfung mit zuverlässiger Auswertung des VARL-Berichts
- geschlossenes Analyseschema 2 mit getrennten Achsen für offizielle Konformität, interne Prüfung und
  technischen Verarbeitungsabschluss
- versionierte Dokumenttypauflösung für 62 UNTDID-1001-Codes aus CEN EN 16931 1.3.15 sowie abgeleitete
  Dokument-, Gläubiger-/Schuldner- und erwartete Zahlungsrollen
- übersichtliche Rechnungsdarstellung mit 30 wesentlichen Kopffakten, expliziter Rechnungsart sowie getrenntem
  Dokument- und erwartetem Zahlungsfluss
- kompakte USt-Angabe je Rechnungsposition mit hervorgehobenem Steuersatz und kleinerem Kategoriecode;
  Abweichungshinweise erscheinen nur bei fehlenden Kombinationen in der Steueraufschlüsselung
- Schema-2-JSON-Analyse, bytegetreuer XML-Export, eigenständige HTML-Berichte in lesbarem oder vollständigem
  Umfang sowie paginierter PDF-Bericht
- lokale HTTP-API mit OpenAPI-Dokumentation
- Docker-Konfiguration, automatisierte Tests, Typprüfung, Linting, Coverage und Release-Build
- vorbereitetes GitHub-Repository mit CI, CodeQL, Dependency Audit, Dependabot, Issue- und Pull-Request-Vorlagen
- repository-weite Codex-Anweisungen in `AGENTS.md`

## Schnellstart für die Nutzung

Für den Start aus dem Quellcode oder als Python-Paket wird Python 3.11 oder neuer benötigt. Die
Windows-x64-Installer bringen Python, Java und KoSIT mit; für den Containerstart wird Docker benötigt.

### Windows-x64-Installer

Die signierten Windows-Installer aus einem GitHub Release benötigen weder Python noch Java und bringen die
festgeschriebenen KoSIT-/XRechnung-Komponenten mit. Es gibt zwei alternative Betriebsarten:

- Der bestehende benutzerbezogene **Desktop-/Tray-Installer** benötigt keine Administratorrechte, installiert
  unter `%LOCALAPPDATA%` und kann die Anwendung nach der Benutzeranmeldung automatisch starten.
- Der neue administrative **Dienst-Installer** installiert unveränderliche Dateien unter `%ProgramFiles%`,
  Maschinenzustand unter `%ProgramData%` und kann das Backend bereits vor einer Benutzeranmeldung als
  `LocalService` starten.

Beide Varianten bieten Browseroberfläche und lokale API, dürfen aber nicht gleichzeitig als Backend laufen.
Der Dienst-Installer migriert keine vorhandene Desktopinstallation und übernimmt deren API-Token nicht.
Vor der Dienstinstallation muss der Desktopmodus einschließlich Autostart vollständig deinstalliert werden; vor
der Desktopinstallation muss entsprechend der Dienstmodus deinstalliert sein.
Details zu Auswahl, Installation, Signierung und Prüfung stehen in
[`docs/WINDOWS_PACKAGE.md`](docs/WINDOWS_PACKAGE.md).

### Windows aus dem Quellcode

```bat
scripts\start.bat
```

### Linux oder macOS

```sh
chmod +x scripts/start.sh
./scripts/start.sh
```

Danach ist die Anwendung standardmäßig unter `http://127.0.0.1:8080` erreichbar.

Die Startskripte öffnen den Browser automatisch. Ist `EINVOICE_API_TOKEN` gesetzt, richtet das geöffnete
Bootstrapfenster eine vom dauerhaften API-Token getrennte Browsersitzung ein.

### Installation als Python-Paket

```sh
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install .
e-rechnung-pruefer --open
```

### Docker

```sh
docker compose up --build
```

Der Port wird in `compose.yaml` ausschließlich an `127.0.0.1` gebunden. Das lokale `vendor/`-Verzeichnis wird eingebunden, damit eine optionale KoSIT-Installation erhalten bleibt.

## Oberfläche und Berichtsausgaben

- **Prüfbericht JSON** lädt die vollständige Schema-2-Analyse.
- **XML speichern** exportiert die ausgewählte Rechnungs-XML bytegetreu.
- **HTML-Bericht** erzeugt den eigenständigen Bericht mit `scope=readable`.
- **Vollständiger Bericht** erzeugt den HTML-Bericht mit `scope=complete` einschließlich technischer Anhänge.
- **Drucken / PDF** lädt den lesbaren HTML-Bericht und öffnet den Browserdruck. Der eigenständige paginierte
  PDF-Bericht für Automatisierungen wird dagegen über `POST /api/report/pdf` erzeugt.

Die sichtbare Kennzeichnung „Ausgewertet“, „Mit Hinweisen“ oder „Handlungsbedarf“ fasst die Darstellung für
Menschen zusammen. Sie ist keine vierte API-Achse und darf von Integrationen nicht anstelle von
`assessment.official`, `assessment.internal` und `assessment.processing` ausgewertet werden.

## Entwicklungsumgebung

### Linux oder macOS

```sh
./scripts/bootstrap_dev.sh
. .venv/bin/activate
python -m app --reload
```

### Windows PowerShell

```powershell
.\scripts\bootstrap_dev.ps1
.\.venv\Scripts\Activate.ps1
python -m app --reload
```

Vollständige Qualitätsprüfung:

```sh
./scripts/check.sh
```

oder unter Windows:

```powershell
.\scripts\check.ps1
```

Der Check umfasst Versionskonsistenz, Ruff, Mypy sowie Pytest mit Branch Coverage. Mit `python scripts/build_release.py` entstehen Wheel, Source Distribution, ein bereinigtes Repository-ZIP und SHA-256-Prüfsummen.

Die Windows-x64-Installer werden nativ auf Windows beziehungsweise im Windows-Job von GitHub Actions gebaut. Sie
können auf dem Intel-Mac entwickelt, aber nicht erzeugt oder ausgeführt werden.

## Weiterentwicklung mit Codex

`AGENTS.md` beschreibt Architektur, Sicherheitsinvarianten, Testbefehle und fachliche Grenzen für Codex. Gute Aufgaben nennen das gewünschte Verhalten, eine anonymisierte Reproduktion und die erwarteten Tests. Beispiele und empfohlene Arbeitsabläufe stehen in [`docs/CODEX.md`](docs/CODEX.md).

Vor dem Übernehmen einer Codex-Änderung immer ausführen:

```sh
./scripts/check.sh
```

## Erstes GitHub-Repository anlegen

Nach dem Entpacken kann Git samt erstem Commit vorbereitet werden:

```sh
./scripts/init_git.sh https://github.com/OWNER/REPOSITORY.git
```

Unter Windows:

```powershell
.\scripts\init_git.ps1 -RemoteUrl https://github.com/OWNER/REPOSITORY.git
```

Anschließend Änderungen kontrollieren und pushen:

```sh
git status
git push -u origin main
```

Alternativ stehen die manuellen Schritte und Hinweise für Branch-Schutz, Actions und Releases in [`docs/GITHUB_SETUP.md`](docs/GITHUB_SETUP.md).

## KoSIT-Validator einrichten

Die interne Prüfung funktioniert ohne Java. In Quell-, Wheel- und Repository-Paketen werden der KoSIT-Validator
und die XRechnung-Konfiguration bewusst nicht mitgeliefert. Der folgende Einrichtungsbefehl steht im Quell-,
Repository- und entpackten Source-Distribution-Paket zur Verfügung; ein allein installiertes Wheel enthält das
Repository-Skript nicht. Der Windows-x64-Installer bringt dagegen die beim Build festgeschriebenen und
verifizierten Versionen samt Java-Laufzeit mit.

```sh
python scripts/install_kosit.py
```

Aktualisierung einer vorhandenen Installation:

```sh
python scripts/install_kosit.py --force
```

Der Installer:

- lädt ausschließlich ein `validator-<Version>-standalone.jar`;
- prüft das JAR-Manifest auf `Main-Class`;
- installiert ausschließlich die in `packaging/kosit/components.lock.json` festgelegten Artefakte;
- prüft deren verpflichtende SHA-256-Prüfsummen vor der Installation;
- installiert die XRechnung-Szenarien nach `vendor/kosit/`;
- schreibt die lokale, von Git ausgeschlossene Datei `.env.kosit`.

Die Anwendung verwendet KoSIT ohne `-p/--print`, liest primär die erzeugte `*-report.xml` und wertet die ausdrückliche VARL-Entscheidung `<rep:accept/>` oder `<rep:reject/>` aus. Java-, JAR-, Konfigurations- und Timeoutfehler werden als „nicht ausgeführt“ und nicht als Rechnungsablehnung ausgewiesen.

Die Sperrdatei [`packaging/kosit/components.lock.json`](packaging/kosit/components.lock.json) legt derzeit
KoSIT Validator **1.6.2** und die XRechnung-Validator-Konfiguration **2026-01-31** für XRechnung **3.0.2**
fest. Darin enthalten sind CEN-EN-16931-Regeln **1.3.15** und XRechnung-Schematron **2.5.0**. Installer und
Windows-Build prüfen die festgelegten SHA-256-Werte; `/api/health` veröffentlicht die Komponentenversionen ohne
lokale Pfade.

## Steuerkategorien

Die Ansicht zeigt für jede Steuergruppe gleichzeitig:

- den maschinenlesbaren Kategoriecode und seine Bezeichnung;
- den im XML tatsächlich angegebenen Steuersatz; unzulässige Kombinationen bleiben sichtbar und erzeugen einen
  Befund;
- den Basis- beziehungsweise Kategorienettobetrag;
- den Befreiungs- oder Begründungstext;
- einen Befreiungsgrundcode.

Damit wird ein vorhandener Begründungstext nicht mehr durch die Anzeige der Bemessungsgrundlage verdrängt. Für Kategorie `O` wird kein künstlicher Satz von `0 %` dargestellt; ein dennoch vorhandener Steuersatz wird als Fehler gemeldet. Eine Kombination wie `G` mit dem Text „nicht im Inland steuerbar“ erzeugt eine transparente semantische Warnung, weil Code und Begründung unterschiedliche Geschäftsvorfälle beschreiben können. Details stehen in [`docs/TAX_CATEGORIES.md`](docs/TAX_CATEGORIES.md).

## API

Interaktive Dokumentation: `http://127.0.0.1:8080/api/docs`

| Methode | Endpunkt | Zweck |
|---|---|---|
| `GET` | `/api/health` | Anwendungsversion und KoSIT-Bereitschaft |
| `GET` | `/api/examples/{cii|ubl}` | anonymisierte Beispieldatei |
| `POST` | `/api/analyze` | geschlossenes Analyseschema 2 mit drei unabhängigen Bewertungsachsen |
| `POST` | `/api/report` | eigenständiger HTML-Bericht; standardmäßig `scope=readable` |
| `POST` | `/api/report/pdf` | eigenständiger PDF-Bericht; standardmäßig `scope=readable` |
| `POST` | `/api/xml` | ursprüngliche oder aus PDF extrahierte XML bytegetreu |

Beispiel:

```sh
curl -F "file=@rechnung.xml" -F "official=false" \
  http://127.0.0.1:8080/api/analyze > pruefbericht.json
```

Sobald `EINVOICE_API_TOKEN` gesetzt ist – in installierten Windows-Betriebsarten immer – benötigen direkte
Aufrufe von `/api/*` den Bearer-Header; nur `/api/health` bleibt öffentlich. Das Token gehört nicht in die URL
oder ein Skript-Repository:

```sh
curl -H "Authorization: Bearer ${EINVOICE_API_TOKEN}" \
  -F "file=@rechnung.xml" -F "official=false" \
  http://127.0.0.1:8080/api/analyze > pruefbericht.json
```

Erfolgreiche JSON-Antworten von `POST /api/analyze` enthalten ausschließlich Schema 2 und beginnen mit
`"schema_version": 2`. Die drei Statusachsen liegen unter:

- `assessment.official`: offizielle Konformitätsentscheidung;
- `assessment.internal`: interne Vorprüfungen und Plausibilitätskontrollen;
- `assessment.processing`: Vollständigkeit der technischen Verarbeitung.

Diese Achsen dürfen nicht wieder zu einem einzigen `ok`-/`warning`-/`invalid`-Wert verdichtet werden.

`POST /api/report` liefert den eigenständigen HTML-Bericht, `POST /api/report/pdf` einen direkt öffnungsfähigen
PDF-Bericht für Mail-Automatisierungen. Beide Endpunkte akzeptieren den Form-Parameter
`scope=readable|complete`: Ohne Angabe wird der menschenlesbare Bericht (`readable`) erzeugt; nur
`scope=complete` ergänzt technische XML-Felder, XML-Darstellungen und KoSIT-Rohdaten. Beispiel:

```sh
curl -F "file=@rechnung.xml" -F "official=false" -F "scope=complete" \
  -o vollstaendiger-pruefbericht.html http://127.0.0.1:8080/api/report
```

Als anwendungsspezifische Analyseheader senden beide Antworten
`X-Einvoice-Analysis-Schema`, `X-Einvoice-Syntax`, `X-Einvoice-Conformity-Status`,
`X-Einvoice-Internal-Status`, `X-Einvoice-Processing-Status` und `X-Einvoice-Report-Scope`. Die früheren Header
`X-Einvoice-Validation-Status` und `X-Einvoice-Official-Status` werden nicht mehr gesendet. Die
von diesen API-Endpunkten gelieferten `Content-Disposition`-Dateinamen enthalten keine Rechnungs- oder
Originaldateikennung.

Das konkrete Alt→Neu-Mapping steht in [`docs/API_MIGRATION_V2.md`](docs/API_MIGRATION_V2.md). Verbindliche
Automatisierungs- und Routingregeln beschreibt
[`docs/AUTOMATION_INTEGRATION.md`](docs/AUTOMATION_INTEGRATION.md).

Die installierten Windows-Betriebsarten binden ausschließlich an `127.0.0.1` auf dem festen Port `8080`
beziehungsweise dem konfigurierten Port. Der Desktopmodus speichert sein persistentes API-Token unter
`%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt`; der Dienst verwendet die geschützte Maschinendatei
`%ProgramData%\E-Rechnungs-Pruefer\api-token.txt`. Das Token darf nur kontrolliert in den Credential-Speicher
oder die geschützte Prozessumgebung von Node-RED provisioniert werden, niemals in einen exportierten Flow oder
eine URL.

Mit `E-Rechnungs-Pruefer.exe --background` startet der Desktopmodus ohne automatisches Browserfenster. Sein
optionaler HKCU-Autostart gilt erst ab Benutzeranmeldung und bleibt eine eigenständige, nicht privilegierte
Alternative zum Dienst. Im Dienstmodus öffnet der Startmenüeintrag **E-Rechnungs-Prüfer öffnen** die Oberfläche
über authentifizierte lokale IPC und eine kurzlebige Einmalsitzung; das dauerhafte API-Token gelangt dabei nicht
in den Browser.

## Konfiguration

Umgebungsvariablen können in `.env` oder `.env.kosit` stehen. Beide Dateien werden nicht versioniert.

| Variable | Standard | Bedeutung |
|---|---:|---|
| `HOST` | `127.0.0.1` | Bind-Adresse |
| `PORT` | `8080` | HTTP-Port |
| `MAX_UPLOAD_BYTES` | `26214400` | maximale Uploadgröße |
| `MAX_TECHNICAL_ROWS` | `100000` | maximale tabellarische XML-Einträge |
| `MAX_XML_STRUCTURE_ITEMS` | `100000` | harte Obergrenze für XML-Elemente, Attribute, Namespaces, Kommentare und Processing Instructions |
| `MAX_TECHNICAL_SECONDS` | `5.0` | monotones Zeitbudget nur für die technische Tabellenbildung |
| `KOSIT_ENABLED` | `true` | KoSIT-Anbindung aktivieren |
| `KOSIT_JAVA_BIN` | `java` | Java-Befehl |
| `KOSIT_VALIDATOR_JAR` | automatisch | Pfad zum Standalone-JAR |
| `KOSIT_SCENARIOS` | automatisch | Semikolon-getrennte Szenariodateien |
| `KOSIT_REPOSITORIES` | automatisch | Semikolon-getrennte Ressourcenpfade |
| `KOSIT_TIMEOUT_SECONDS` | `60` | Zeitgrenze pro KoSIT-Aufruf |
| `EINVOICE_API_TOKEN` | automatisch in den Windows-Laufzeiten | optional vorgegebenes URL-sicheres ASCII-API-Token mit mindestens 32 Zeichen |

Der Windows-Dienst liest Port und KoSIT-Einstellungen stattdessen aus der streng validierten
`%ProgramData%\E-Rechnungs-Pruefer\service.json` und aktiviert diese Werte zusammen mit dem Maschinentoken vor
dem Import der Webanwendung. Seine Bind-Adresse ist fest auf `127.0.0.1` gesetzt und nicht konfigurierbar.

Ist `EINVOICE_API_TOKEN` beim interaktiven Quellstart mit `--open` gesetzt, erzeugt die Anwendung für den
geöffneten Browser automatisch eine davon getrennte Sitzung. Das dauerhafte Bearer-Token wird weder an den
Browser noch an JavaScript übergeben; API-Automatisierungen verwenden es weiterhin im
`Authorization: Bearer ...`-Header.

## Sicherheit und Datenschutz

- Standardmäßig Bindung nur an `127.0.0.1`
- keine dauerhafte Speicherung von Uploads
- Verarbeitung des KoSIT-Aufrufs in einem temporären Verzeichnis
- Ablehnung von DTD- und ENTITY-Deklarationen
- deaktivierte externe Entitäten, DTD-Nachladung und XML-Netzwerkzugriffe
- begrenzte Upload-, XML-Struktur- und Darstellungsgrößen sowie lineare technische XML-Pfade
- bereinigte Download-Dateinamen, Sicherheitsheader und Content Security Policy
- persistentes Bearer-Token für `/api/*` in den installierten Windows-Modi; `/api/health` bleibt die öffentliche
  Ausnahme
- vom API-Token getrennte Browsersitzung sowie Host- und Origin-Prüfung im Windows-Desktop-Modus
- inhaltsadressierte UI-Assets und ein Revisionsvertrag, der alte offene Tabs kontrolliert mit HTTP 409 oder
  nach einem Prozessneustart mit HTTP 403 samt Wiederöffnungshinweis stoppt; Bearer-API-Clients bleiben davon
  ausgenommen
- geschützte ProgramData-DACLs, dienstspezifischer SID und einmaliger, authentifizierter
  IPC-Browserbootstrap im Windows-Dienstmodus
- nicht privilegierter Benutzer im Docker-Image

Ein öffentlicher oder mehrbenutzerfähiger Betrieb benötigt zusätzlich Authentifizierung, TLS, Rate Limits, sichere Protokollierung, Malware-Prüfung und Ressourcenbegrenzung. Siehe [`SECURITY.md`](SECURITY.md) und [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).

## Dokumentation

- [`START_HERE.txt`](START_HERE.txt) – kompakter Einstieg, Installerwahl und aktuelle Upgradehinweise
- [`CHANGELOG.md`](CHANGELOG.md) – kuratierte Änderungen und Breaking Changes je Version
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) – Komponenten und Datenfluss
- [`docs/API_MIGRATION_V2.md`](docs/API_MIGRATION_V2.md) – sofortige Migration von Analyseschema 1 auf 2
- [`docs/VALIDATION.md`](docs/VALIDATION.md) – interne und offizielle Prüfung
- [`docs/AUTOMATION_INTEGRATION.md`](docs/AUTOMATION_INTEGRATION.md) – verbindliche Status- und Fehlerregeln für Node-RED und andere Automatisierungen
- [`docs/NODE_RED.md`](docs/NODE_RED.md) – Import und Konfiguration des anonymisierten Node-RED-Beispielflows
- [`docs/TAX_CATEGORIES.md`](docs/TAX_CATEGORIES.md) – Darstellung und Plausibilitätsregeln
- [`docs/CODEX.md`](docs/CODEX.md) – Arbeit mit Codex
- [`docs/GITHUB_SETUP.md`](docs/GITHUB_SETUP.md) – Repository, Actions und Branch-Schutz
- [`docs/RELEASE.md`](docs/RELEASE.md) – Versionierung und Veröffentlichung
- [`docs/WINDOWS_PACKAGE.md`](docs/WINDOWS_PACKAGE.md) – Windows-Desktop- und Dienstmodus, Installer, Signierung und Pakettests
- [`CONTRIBUTING.md`](CONTRIBUTING.md) – Beiträge und Pull Requests

## Lizenz

MIT. Optionale beziehungsweise im Windows-Paket gebündelte KoSIT-, XRechnung- und Java-Komponenten behalten ihre jeweiligen Lizenz- und NOTICE-Bedingungen. Siehe [`THIRD_PARTY.md`](THIRD_PARTY.md).
