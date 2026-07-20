# E‑Rechnungs‑Viewer & Prüfer

Lokale Webanwendung zum Öffnen, verständlichen Darstellen und Prüfen strukturierter E‑Rechnungen. Sie liest XML-Rechnungen direkt oder extrahiert die Rechnungs-XML aus einer Hybrid-PDF. Neben einer lesbaren Rechnungsansicht entstehen ein gemeinsamer Prüfbericht, XML-Textansichten und ein navigierbarer technischer Tabellenanhang mit erfassten Elementwerten, Attributen, Namespace-URIs und Pfaden.

Die Anwendung ist als nachvollziehbares Prüf- und Analysewerkzeug konzipiert. Die eingebaute Prüfung ersetzt weder eine fachliche Steuerberatung noch eine vollständige Profilvalidierung. Für XSD-/Schematron-Prüfungen kann der offizielle KoSIT-Validator angebunden werden.

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
- JSON-, XML- und eigenständiger HTML-Bericht; PDF-Ausgabe über den Browserdruck
- lokale HTTP-API mit OpenAPI-Dokumentation
- Docker-Konfiguration, automatisierte Tests, Typprüfung, Linting, Coverage und Release-Build
- vorbereitetes GitHub-Repository mit CI, CodeQL, Dependency Audit, Dependabot, Issue- und Pull-Request-Vorlagen
- repository-weite Codex-Anweisungen in `AGENTS.md`

## Schnellstart für die Nutzung

Voraussetzung ist Python 3.11 oder neuer.

### Windows-x64-Installer

Der signierte Windows-Installer aus einem GitHub Release benötigt weder Python noch Java und bringt die festgeschriebenen KoSIT-/XRechnung-Komponenten mit. Details zu Erstellung, Signierung und Prüfung stehen in [`docs/WINDOWS_PACKAGE.md`](docs/WINDOWS_PACKAGE.md).

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

Der Windows-x64-Installer wird nativ auf Windows beziehungsweise im Windows-Job von GitHub Actions gebaut. Er kann auf dem Intel-Mac entwickelt, aber nicht erzeugt oder ausgeführt werden.

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

Die interne Prüfung funktioniert ohne Java. In Quell-, Wheel- und Repository-Paketen werden der KoSIT-Validator und die XRechnung-Konfiguration bewusst nicht mitgeliefert, sondern auf ausdrücklichen Aufruf installiert. Der Windows-x64-Installer enthält dagegen die beim Build festgeschriebenen und verifizierten Versionen samt Java-Laufzeit.

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
- prüft eine veröffentlichte SHA-256-Prüfsumme, sofern vorhanden;
- installiert die XRechnung-Szenarien nach `vendor/kosit/`;
- schreibt die lokale, von Git ausgeschlossene Datei `.env.kosit`.

Die Anwendung verwendet KoSIT ohne `-p/--print`, liest primär die erzeugte `*-report.xml` und wertet die ausdrückliche VARL-Entscheidung `<rep:accept/>` oder `<rep:reject/>` aus. Java-, JAR-, Konfigurations- und Timeoutfehler werden als „nicht ausgeführt“ und nicht als Rechnungsablehnung ausgewiesen.

## Steuerkategorien und Version 1.1

Die Ansicht zeigt für jede Steuergruppe gleichzeitig:

- den maschinenlesbaren Kategoriecode und seine Bezeichnung;
- den Steuersatz, sofern im XML vorhanden und für die Kategorie zulässig;
- den Basis- beziehungsweise Kategorienettobetrag;
- den Befreiungs- oder Begründungstext;
- einen Befreiungsgrundcode.

Damit wird ein vorhandener Begründungstext nicht mehr durch die Anzeige der Bemessungsgrundlage verdrängt. Für Kategorie `O` wird kein künstlicher Satz von `0 %` dargestellt; ein dennoch vorhandener Steuersatz wird als Fehler gemeldet. Eine Kombination wie `G` mit dem Text „nicht im Inland steuerbar“ erzeugt eine transparente semantische Warnung, weil Code und Begründung unterschiedliche Geschäftsvorfälle beschreiben können. Details stehen in [`docs/TAX_CATEGORIES.md`](docs/TAX_CATEGORIES.md).

## API

Interaktive Dokumentation: `http://127.0.0.1:8080/api/docs`

| Methode | Endpunkt | Zweck |
|---|---|---|
| `GET` | `/api/health` | Anwendungsversion und KoSIT-Konfiguration |
| `GET` | `/api/examples/{cii|ubl}` | anonymisierte Beispieldatei |
| `POST` | `/api/analyze` | normalisiertes Modell und gemeinsamer Prüfbericht als JSON |
| `POST` | `/api/report` | eigenständiger HTML-Bericht |
| `POST` | `/api/xml` | ursprüngliche oder aus PDF extrahierte XML bytegetreu |

Beispiel:

```sh
curl -F "file=@rechnung.xml" -F "official=false" \
  http://127.0.0.1:8080/api/analyze > pruefbericht.json
```

## Konfiguration

Umgebungsvariablen können in `.env` oder `.env.kosit` stehen. Beide Dateien werden nicht versioniert.

| Variable | Standard | Bedeutung |
|---|---:|---|
| `HOST` | `127.0.0.1` | Bind-Adresse |
| `PORT` | `8080` | HTTP-Port |
| `MAX_UPLOAD_BYTES` | `26214400` | maximale Uploadgröße |
| `MAX_TECHNICAL_ROWS` | `100000` | maximale tabellarische XML-Einträge |
| `KOSIT_ENABLED` | `true` | KoSIT-Anbindung aktivieren |
| `KOSIT_JAVA_BIN` | `java` | Java-Befehl |
| `KOSIT_VALIDATOR_JAR` | automatisch | Pfad zum Standalone-JAR |
| `KOSIT_SCENARIOS` | automatisch | Semikolon-getrennte Szenariodateien |
| `KOSIT_REPOSITORIES` | automatisch | Semikolon-getrennte Ressourcenpfade |
| `KOSIT_TIMEOUT_SECONDS` | `60` | Zeitgrenze pro KoSIT-Aufruf |

## Sicherheit und Datenschutz

- Standardmäßig Bindung nur an `127.0.0.1`
- keine dauerhafte Speicherung von Uploads
- Verarbeitung des KoSIT-Aufrufs in einem temporären Verzeichnis
- Ablehnung von DTD- und ENTITY-Deklarationen
- deaktivierte externe Entitäten, DTD-Nachladung und XML-Netzwerkzugriffe
- begrenzte Upload- und Darstellungsgrößen
- bereinigte Download-Dateinamen, Sicherheitsheader und Content Security Policy
- zufällige Sitzung, Host- und Origin-Prüfung im Windows-Desktop-Modus
- nicht privilegierter Benutzer im Docker-Image

Ein öffentlicher oder mehrbenutzerfähiger Betrieb benötigt zusätzlich Authentifizierung, TLS, Rate Limits, sichere Protokollierung, Malware-Prüfung und Ressourcenbegrenzung. Siehe [`SECURITY.md`](SECURITY.md) und [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).

## Dokumentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) – Komponenten und Datenfluss
- [`docs/VALIDATION.md`](docs/VALIDATION.md) – interne und offizielle Prüfung
- [`docs/TAX_CATEGORIES.md`](docs/TAX_CATEGORIES.md) – Darstellung und Plausibilitätsregeln
- [`docs/CODEX.md`](docs/CODEX.md) – Arbeit mit Codex
- [`docs/GITHUB_SETUP.md`](docs/GITHUB_SETUP.md) – Repository, Actions und Branch-Schutz
- [`docs/RELEASE.md`](docs/RELEASE.md) – Versionierung und Veröffentlichung
- [`docs/WINDOWS_PACKAGE.md`](docs/WINDOWS_PACKAGE.md) – Windows-Launcher, Installer, Signierung und Pakettest
- [`CONTRIBUTING.md`](CONTRIBUTING.md) – Beiträge und Pull Requests

## Lizenz

MIT. Optionale beziehungsweise im Windows-Paket gebündelte KoSIT-, XRechnung- und Java-Komponenten behalten ihre jeweiligen Lizenz- und NOTICE-Bedingungen. Siehe [`THIRD_PARTY.md`](THIRD_PARTY.md).
