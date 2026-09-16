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

## Versionsentscheidungen und Dependabot

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
Die Profile enthalten 53 Windows-Pakete, 97 Source-/Dev-/Build-Pakete und jeweils 28 Containerpakete.
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
