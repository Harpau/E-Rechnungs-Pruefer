# Abhängigkeiten pflegen und prüfen

`pyproject.toml` beschreibt kompatible Installationsbereiche für Python 3.11 bis
3.14. Release-Locks legen dagegen eine konkrete, gemeinsam auflösbare Paketmenge
für ein bestimmtes CPython-/OS-/Architekturziel fest. Neu veröffentlichte Versionen
ändern einen bestehenden Release-Kandidaten nicht automatisch.

## Verbindliche Auditabdeckung

Der Workflow `Dependency audit` läuft bei Pull Requests, Pushes auf `main`,
manuell und dienstags um 05:31 UTC. Seine Jobs prüfen unabhängig voneinander:

| Profil | Prüfung |
| --- | --- |
| Runtime und Entwicklung | Jeweils frische Umgebungen auf Ubuntu 24.04, Windows 2022 und macOS 14 mit Python 3.11, 3.12, 3.13 und 3.14; insgesamt 24 Kombinationen. |
| Windows-Release | Plattformunabhängige Offlineprüfung des markerfreien Locks samt Sidecar; anschließend Audit jedes Pins ohne Linux-Neuauflösung. Die native Wheelprüfung erfolgt zusätzlich im Windows-Build. |
| Source-Release | Frische Linux-x64-Umgebung mit CPython 3.14.7 aus dem vollständigen gehashten Source-Lock; Prüfung der Wheelbytes, Zielkompatibilität, Extras und installierten Paketmenge. |
| Docker | Wiederverwendbarer Docker-Workflow für die finalen Linux-amd64- und Linux-arm64-Images; Prüfung ihrer tatsächlichen Inventare. |
| Projektauflösung | Zusätzlicher `pip-audit --strict .`-Lauf für frisch aufgelöste Runtime-Abhängigkeiten. Er ersetzt keinen der anderen Nachweise. |

Bootstrap- und Buildwerkzeuge sind ausdrücklich enthalten. In der allgemeinen
Matrix bleiben Ziel- und Auditwerkzeuge in getrennten virtuellen Umgebungen;
**beide Inventare werden auditiert**. Im Source-Release enthält der vollständige
Lock auch die Dev-, Build- und Auditwerkzeuge. Das eigene Projekt wird ohne
Buildisolation installiert. Nur seine anhand von Paketname und genauem lokalen
Projektpfad bestätigte editable-Installation darf aus dem Fremdpaketinventar
entfallen. Andere editable-Pakete führen zum Fehler.

`scripts/dependency_audit.py` erfasst alle installierten Distributionen und prüft
anschließend genau dieses Inventar ohne zusätzliche Dependency-Auflösung. Fehlende
oder übersprungene Pakete, Netzwerkfehler und unvollständige Berichte sind Fehler.
Der Gesamtjob `Complete dependency audit gate` ist nur erfolgreich, wenn sämtliche
erforderlichen Jobs erfolgreich sind; auch übersprungene oder abgebrochene Jobs
sperren ihn. Matrixjobs werden durch den Fehler eines anderen Matrixjobs nicht
abgebrochen. Inventare, Auditberichte, Werkzeugversionen und verfügbare Fehlerlogs
werden auch bei fehlgeschlagenen Prüfungen 14 Tage als Workflowartefakte aufbewahrt.

## Locks gezielt aktualisieren

`scripts/dependency_lock.py refresh` löst ausschließlich auf dem nativen Ziel mit
der exakt angegebenen regulären CPython-Version neu auf. Der Resolver berücksichtigt
bereits installierte Pakete nicht und lässt nur Wheels zu. Eine Aktualisierung
benötigt daher native Windows-x64-, Linux-x64- bzw. Linux-arm64-Ausführung; eine
Linux-Auflösung mit behauptetem Windows-Ziel genügt nicht.

| Profil | Lockdatei | Explizite Bootstrap-Eingaben |
| --- | --- | --- |
| `windows-release` | `packaging/windows/requirements-release.txt` | `pip==26.2.1`, `setuptools==84.0.0`, `wheel==0.48.0` |
| `source-release` | `packaging/python/requirements-source-release.txt` | `pip==26.2.1`, `setuptools==84.0.0`, `wheel==0.48.0` |
| `docker-amd64` | `packaging/docker/requirements-linux-amd64.txt` | `pip==26.2.1` |
| `docker-arm64` | `packaging/docker/requirements-linux-arm64.txt` | `pip==26.2.1` |

Beispiel auf dem passenden Linux-x64-Ziel mit CPython 3.14.7 und den freigegebenen
Generatorwerkzeugen pip 26.2.1 und packaging 26.3:

```sh
python scripts/dependency_lock.py refresh \
  --profile source-release --python-version 3.14.7 \
  --output packaging/python/requirements-source-release.txt \
  --bootstrap pip==26.2.1 --bootstrap setuptools==84.0.0 --bootstrap wheel==0.48.0
```

Jede Lockdatei wird zusammen mit `<lockdatei>.metadata.json` aktualisiert. Der Sidecar
bindet Eingabedateien, Generatorrevision, Resolverversion, Zielumgebung, Wheel-URLs,
SHA-256-Hashes und Abhängigkeitsmetadaten. Nach Änderungen an Eingaben oder Generator
müssen die betroffenen Locks neu erzeugt werden. Handbearbeitung einzelner Pins
ohne passende Metadaten ist keine gültige Aktualisierung.

`check` prüft diese Bindungen und den vollständigen Dependency-Abschluss offline.
`verify` lädt ausschließlich die bereits festgelegten Wheeldateien, verifiziert
deren Bytes und Metadaten und löst keine neuen Versionen auf:

```sh
python scripts/dependency_lock.py check --lock packaging/python/requirements-source-release.txt
python scripts/dependency_lock.py verify --lock packaging/python/requirements-source-release.txt --installed
```

`--installed --python /pfad/zum/zielpython` erfasst eine getrennte Zielumgebung.
Für Container kann das ausschließlich standardbibliotheksbasierte
`dependency_audit.py capture` im Image ausgeführt und dessen JSON anschließend
extern mit `verify --inventory` und `dependency_audit.py audit` geprüft werden.
Auditwerkzeuge müssen dadurch nicht in ein Produktimage installiert werden.

### Container: getrennte Build- und Laufzeitpakete

Die beiden ursprünglichen Docker-Locks bleiben die nativ verifizierte Wheelquelle.
`scripts/docker_runtime_lock.py` leitet daraus `requirements-runtime-{amd64,arm64}.txt`
ab: Ausschließlich die explizite Pip-Bootstraproot und das Pip-Paket entfallen.
Der Abhängigkeitsabschluss wird erneut geprüft; benötigt ein Laufzeitpaket Pip,
schlägt die Ableitung fehl. Der eigene Sidecar bindet Parent-Lock, Parent-Sidecar,
Generatorrevision, Ableitungsskript und unveränderte Wheelmetadaten. Nach einer
Änderung des Ableitungsskripts müssen beide Runtime-Locks neu erzeugt werden:

```sh
python scripts/docker_runtime_lock.py derive \
  --parent packaging/docker/requirements-linux-amd64.txt \
  --output packaging/docker/requirements-runtime-amd64.txt
```

Für arm64 wird derselbe Aufruf mit den entsprechenden Dateinamen ausgeführt.
Bestehende abweichende Ausgaben werden nicht still überschrieben. Nach bewusstem
Entfernen der betroffenen generierten Dateien erfolgt die erneute Ableitung aus
den validierten Parent-Dateien, ohne Paketauflösung.

Der Builder installiert nur die gehashten Pip-/Packaging-Werkzeuge aus
`requirements-builder.txt`; sein vollständiges Inventar wird separat auditiert.
Er prüft alle Parent-Wheelbytes nativ und installiert die 27 Runtime-Pakete in ein
Pip-freies Venv. Das finale Image erhält den offiziellen CPython 3.14.7, dieses
unverändert platzierte Venv und die stabile Debian-13-Java-Laufzeit. Java bleibt
als Debian-Paket inventarisiert und vom OS-Scanner abgedeckt.

`scripts/build_container_rootfs.py` übernimmt nur den benötigten Laufzeitbestand
einschließlich transitiver ELF-Bibliotheken, dynamischer Provider, Zertifikate,
Zeitzonen und Lizenzen. Globale Buildpakete und der vollständige ungenutzte
`ensurepip`-Baum einschließlich seines eingebetteten Pip-Wheels werden tatsächlich
weggelassen. Ebenso entfallen die ungenutzten Python-GUI-Komponenten Tkinter,
IDLE und Turtle: Die offizielle Slim-Basis liefert bereits keine Tk-Laufzeit,
obwohl sie die native `_tkinter`-Erweiterung enthält. Für sämtliche übrigen
übernommenen ELF-Dateien bleibt die Abhängigkeitsprüfung verpflichtend.
Paketstatusdaten für erhaltene Debian-Dateien bleiben vollständig
erhalten, auch wenn nur Teile eines Pakets benötigt werden. Das Dateimanifest
dokumentiert Herkunft, Inhalt und ELF-Abhängigkeiten des Laufzeitunterbaus;
Anwendungsdateien werden separat durch Commit und finales Image gebunden.

Die originale paketgebundene `/etc/debian_version` bleibt erhalten, damit Trivy
Debian tatsächlich erkennt. Beim Inventarvergleich werden Epoch, Version und
Debian-Revision sowohl für Binär- als auch Quellpakete vollständig berücksichtigt.
Die von Docker bereitgestellten drei Netzwerkdateien werden über Gerät und Inode
an den gebundenen Container gekoppelt; nur der exakte Laufzeitlink
`/etc/mtab -> /proc/mounts` ist zusätzlich zulässig. Standardisierte `WHEEL`-Dateien
sind ausschließlich innerhalb ihrer vollständig gebundenen Paketmetadaten erlaubt.

Die finale Inventur muss exakt zum Runtime-Lock passen. Zusätzliche native Proben
prüfen Benutzeridentität, fehlende Buildwerkzeuge, CA-/Java-Truststore, DNS,
Zeitzonen sowie Unicode-PDFs und native Bildbibliotheken. Nur ein eigener expliziter
KoSIT-Setup-Prozess bekommt Netzwerkzugriff zum Download der hashgebundenen
öffentlichen Komponenten. Die anschließenden Rechnungs- und KoSIT-Funktionstests
laufen ohne externes Netzwerk. Es gibt keine Severity-, Unfixed- oder CVE-Ausnahmen:
Verbleibende Bibliotheksbefunde blockieren den Gesamtgate weiterhin.

Für die Entwicklung des Containers kann der CI-Workflow manuell mit
`container_only=true` auf einem eigenen Probe-Branch gestartet werden. Dieser
begrenzte Lauf überspringt Windows-Paketmutationen und ist kein vollständiger
Release-Nachweis. Die normalen PR-/Main-Läufe prüfen weiterhin alle Jobs;
Containerprobe, Lockvorbereitung und Volllauf haben getrennte Concurrency-Gruppen.

Die positive Trivy-Abdeckung bezieht sich auf die behaltenen Debian-Pakete und
installierten Python-Distributionen, der separate JAR-Scan auf die eingebetteten
Maven-Komponenten. Die Herkunftsbindung des offiziellen CPython-Interpreters ist
kein eigener CVE-Scan seines nativen Codes; Interpreter-Advisories und darin
enthaltene Bibliotheken müssen zusätzlich bewertet werden. Ein leeres
Distributionsergebnis allein belegt keine vollständige Unbetroffenheit des Images.

## Versionsentscheidungen und Dependabot

Der gezielte CPython-3.14.7-Backport für CVE-2026-15806 und seine Grenzen für
Quellinstallationen sind in [CPYTHON_SECURITY.md](CPYTHON_SECURITY.md) beschrieben.
Docker-Dateimanifest und Windows-Frozen-Prüfung binden den tatsächlich korrigierten
Standardbibliothekscode; ein Paketversionsaudit allein würde diesen Nachweis nicht liefern.

Aktualisierungen wählen den neuesten gemeinsam verträglichen stabilen Stand.
`Requires-Python`, Wheel-/ABI-Verfügbarkeit, Marker und gekoppelte Pins bleiben
verbindlich; ein transitives Paket darf nicht gegen einen exakten Pin seines
Elternpakets ausgetauscht werden. Abweichungen vom neuesten Einzelpaketstand werden
begründet. Größere Versionssprünge benötigen die betroffenen API- und
Verhaltensregressionen. Eine höhere Python-Mindestversion erfordert eine gemeinsame
Änderung von Metadaten, unterstützter Matrix und Dokumentation.

Dependabot beobachtet das Root-Manifest, die Packaging-Verzeichnisse, GitHub
Actions und Docker. Seine PRs sind Aktualisierungsvorschläge: Der eigene Sidecar
und die gekoppelten nativen Locks werden dadurch nicht zuverlässig neu erzeugt.
Manifeständerungen und betroffene Locks müssen gemeinsam geprüft und aktualisiert
werden; automatische Zusammenführung ist nicht vorgesehen. Hash-, Abschluss-,
Inventar- und Synchronitätsprüfungen bleiben maßgeblich.

Vor einer Übernahme laufen `./scripts/check.sh`, `pip check`, sämtliche Auditprofile
sowie die betroffenen nativen Builds und Regressionen. Die gesonderte
Release-Abnahme folgt [ACCEPTANCE.md](ACCEPTANCE.md); ein grüner Auditlauf ersetzt
weder Produktabnahme noch Signatur- und Artefaktprüfung.

## Wartungsstand 2.0.3 (16.09.2026)

Die native Erzeugung mit CPython 3.14.7 und dem unabhängig geprüften Generator lief auf Windows x64,
Linux x64 und Linux arm64: [Vorbereitungslauf 35126632657](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35126632657).
Die Profile enthalten 53 Windows-Pakete, 97 Source-/Dev-/Build-Pakete und jeweils 28 Pakete in den
ursprünglichen Docker-Locks. Die davon abgeleiteten finalen Container enthalten jeweils 27 Python-Pakete;
Pip und Packaging bleiben in der separat auditierten Build-Stufe.
Die allgemeine Python-Mindestversion bleibt 3.11; für die aktuellen stabilen Pakete ist keine Anhebung nötig.

Der zusätzliche aktuelle PyPI-Abgleich umfasst 110 unterschiedliche Pakete aus diesen Profilen und der
macOS-Entwicklungsumgebung: 109 entsprechen dem neuesten nicht zurückgezogenen stabilen Release.
`pydantic-core==2.46.5` bleibt bewusst mit `pydantic==2.13.5` gekoppelt: dessen veröffentlichte Metadaten
verlangen genau diese Core-Version, obwohl Core 2.49.0 separat bereits verfügbar ist.
Quelle: [Pydantic-2.13.5-Paketmetadaten](https://pypi.org/pypi/pydantic/2.13.5/json).

Die allgemeine Runtime- und Dev-Matrix löst weiterhin frisch innerhalb der unterstützten Bereiche auf.
Das ist ein zusätzlicher Kompatibilitäts-/Sicherheitsnachweis; die ausgelieferten Windows-, Source- und
Containerprofile verwenden ihre vollständigen Locks. Neue Meldungen oder ein unvollständiger Audit
bleiben Fehler. Eine erfolgreiche Paketauflösung allein ist noch keine Release- oder Clientabnahme.
