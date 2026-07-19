# Mitwirken

Beiträge sind willkommen, besonders reproduzierbare Parser-, Darstellungs- und Validierungsverbesserungen mit anonymisierten Tests.

## Entwicklungsumgebung

```sh
./scripts/bootstrap_dev.sh
```

Unter Windows:

```powershell
.\scripts\bootstrap_dev.ps1
```

## Arbeitsablauf

1. Issue oder klar abgegrenzte Aufgabe beschreiben.
2. Branch von `main` erstellen.
3. Verhalten mit einem anonymisierten Regressionstest absichern.
4. Implementierung klein und nachvollziehbar halten.
5. `./scripts/check.sh` beziehungsweise `scripts\check.ps1` ausführen.
6. Pull Request mit Problem, Lösung, Tests und möglichen fachlichen Grenzen eröffnen.

## Anforderungen an Testdaten

Keine echten Rechnungen oder personenbezogenen Daten committen. Namen, Anschriften, E-Mail-Adressen, Konten, Steuerkennungen, Dokumentnummern und Hashes müssen synthetisch sein. XML-Fixtures gehören nach `tests/fixtures/` oder – wenn sie auch über die UI angeboten werden sollen – nach `app/examples/`.

## Fachliche Regeln

Ein bestandenes technisches Schema oder Schematron beweist nicht automatisch die steuerrechtliche Richtigkeit. Interne semantische Hinweise müssen als Heuristiken formuliert werden. Neue Regeln benötigen:

- stabile Regel-ID;
- klare Severity;
- verständlichen deutschen Titel und Text;
- tatsächlichen und erwarteten Wert, soweit sinnvoll;
- mindestens einen positiven und einen negativen Test.

## Code-Stil

Python wird mit Ruff formatiert und geprüft. Mypy prüft die Anwendung und die Release-Skripte. Änderungen an JavaScript oder Templates sollen fokussiert bleiben; großflächige Formatänderungen erschweren die fachliche Review.

## Sicherheit

Sicherheitsprobleme nicht mit echten Rechnungsdaten in öffentlichen Issues veröffentlichen. Hinweise stehen in `SECURITY.md`.
