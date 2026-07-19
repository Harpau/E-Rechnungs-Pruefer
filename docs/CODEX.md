# Weiterentwicklung mit Codex

## Projekt öffnen

Das Repository enthält eine rootweite `AGENTS.md`. Codex soll diese Datei vor Änderungen berücksichtigen. Sie beschreibt Architektur, Sicherheitsinvarianten, fachliche Grenzen und die vollständige Testsequenz.

## Gute Aufgabenformulierung

Eine produktive Aufgabe enthält:

- das beobachtete Verhalten;
- das erwartete Verhalten;
- betroffene Syntax oder Profilkennung;
- eine vollständig anonymisierte minimale XML-Reproduktion;
- die gewünschte Severity oder Anzeige;
- Akzeptanzkriterien und erwartete Tests.

Beispiel:

```text
Bei einer CII-Steuergruppe mit CategoryCode O und ohne RateApplicablePercent
soll die UI keinen Text „0 %“ erzeugen. Basisbetrag und ExemptionReason müssen
gleichzeitig sichtbar bleiben. Ergänze Parser-, HTML- und Validierungstests.
Führe anschließend scripts/check.sh aus.
```

## Empfohlener Ablauf

1. Codex um Analyse der relevanten Dateien bitten.
2. Vor der Implementierung einen Testplan anfordern.
3. Änderung mit Regressionstest erstellen lassen.
4. Diff auf Datenschutz, XML-Sicherheit und fachliche Behauptungen prüfen.
5. Lokal `scripts/check.sh` oder `scripts\check.ps1` ausführen.
6. Nur anonymisierte Testdaten committen.

## Nützliche Befehle

```sh
python -m pytest tests/test_tax_categories.py -q
python -m pytest -k kosit
python -m ruff check app tests scripts
python -m ruff format app tests scripts
python -m mypy
python scripts/build_release.py
```

## Review-Fragen

- Bleiben Original-XML-Bytes unverändert exportierbar?
- Wurde unbekannter XML-Inhalt im technischen Anhang erhalten?
- Kann untrusted Text ungeescaped in HTML gelangen?
- Wird ein technischer KoSIT-Fehler versehentlich als Ablehnung bezeichnet?
- Ist eine neue Steuerregel als Heuristik statt als definitive Rechtsauskunft formuliert?
- Enthält der Diff echte Rechnungs- oder Kontodaten?
- Gibt es Tests für CII und UBL, soweit beide Syntaxen betroffen sind?
- Stimmen alle Versionsangaben und der Changelog?

## Größere Änderungen

Bei neuen Syntaxen oder tiefen Modelländerungen zuerst `docs/ARCHITECTURE.md` aktualisieren und eine kurze Designentscheidung unter `docs/decisions/` anlegen. Bei rein lokalen Fehlerkorrekturen genügt ein fokussierter Test und Changelog-Eintrag.
