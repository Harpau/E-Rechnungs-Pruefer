# Gebundene Abnahmekontexte

`scripts/acceptance_context.py` erzeugt technische Teilnachweise für einen isolierten CI-Job oder ein
VM-Szenario. Es implementiert keine Installer-, VM-, Signatur- oder Zugangsdatenlogik. Der optionale
`run-ci`-Aufruf umschließt ein ausdrücklich angegebenes bestehendes Testskript. Der
vollständige Releaseplan nach [`ACCEPTANCE.md`](ACCEPTANCE.md) referenziert diese Teilnachweise und bleibt für
Testmatrix, Ausgangszustände, bedingte Tests, Klassifikation, Wiederholungsbudget und Closeout verantwortlich.
Jeder Teilnachweis erhält einen neuen, zuvor nicht existierenden Evidence-Unterordner. Historische Läufe
werden weder erweitert noch als neue Autorisierung verwendet.

## Lebenszyklus und CLI

Die CLI verwendet ausschließlich Python-3.11-Standardbibliotheken. Erfolgreiche Aufrufe geben genau ein
JSON-Objekt auf stdout aus; fehlgeschlagene Aufrufe liefern Exitcode 1 und eine deutsche Diagnose auf stderr.
Ein Aufrufer darf nach einem fehlgeschlagenen Guard keine Produktmutation ausführen.

1. `init-ci` bindet den tatsächlichen `git rev-parse HEAD` an `GITHUB_SHA`, Repository, Workflow-Run,
   Run-Attempt, Job und Runnername/-OS/-Architektur. Insbesondere ist bei Pull Requests der tatsächlich
   ausgecheckte Merge-Testcommit maßgeblich. `init` liest stattdessen ein explizites VM-Binding.
2. `issue` bindet eine im Scope erlaubte Aktion und ihre regulären Artefaktdateien mit absolutem Pfad,
   Dateiname, Größe und SHA-256. Das Ergebnis enthält die neue eindeutige `id` des Kontextes.
3. `guard` verifiziert die aktuelle Zielbindung und sämtliche Artefakte unmittelbar vor dem vorhandenen
   Mutationsskript. Es konsumiert den Claim genau einmal. Ein zweiter Guard oder eine gleichzeitig laufende
   Aktion desselben Laufs scheitert. Das aufgerufene Skript muss weiterhin seine vorhandenen unmittelbaren
   Kollisions-, Ziel-, Signatur- und Produktzustandsprüfungen ausführen.
4. `complete` bindet die Klassifikation und kopiert unveränderte, nichtleere Rohbelege in den Evidence-Root.
   `PASS` ist erst nach einem erfolgreichen Guard zulässig. Ein noch nicht konsumierter Kontext kann nur
   `ABORTED` oder `INCONCLUSIVE` abgeschlossen werden, beispielsweise nach Ablauf während einer Benutzerpause.
   Ein Nicht-PASS sperrt sämtliche weiteren Mutationen dieses Teil-Laufs. Abschlussbelege sind auch nach
   Ablauf der Aktionsgültigkeit zulässig; sie erteilen keine weitere Aktionsberechtigung.
5. `verify` prüft Zustand, Ereigniskette und alle abgeschlossenen Rohbelege/Receipts. Der abschließende
   Gesamtbestand wird zusätzlich mit `scripts/release_evidence.py create` und `verify` inventarisiert.

Beispiel für einen frischen CI-Lauf; die Shell muss Exitcodes ausnahmslos prüfen und `CONTEXT_ID` aus dem
JSON-Ergebnis von `issue` übernehmen:

```text
python scripts/acceptance_context.py init-ci --root EVIDENCE --controller CONTROLLER --version VERSION --scope desktop-test
python scripts/acceptance_context.py issue --root EVIDENCE --controller CONTROLLER --action desktop-test --artifact INSTALLER --autonomous-minutes 30
python scripts/acceptance_context.py guard --root EVIDENCE --controller CONTROLLER --context CONTEXT_ID --ci
[gebundenes vorhandenes Pakettestskript ausführen und seine unveränderte Ausgabe aufzeichnen]
python scripts/acceptance_context.py complete --root EVIDENCE --controller CONTROLLER --context CONTEXT_ID --status PASS --evidence RAW_LOG
python scripts/acceptance_context.py verify --root EVIDENCE
```

Alle tatsächlich ausgeführten Dateien, einschließlich eines getrennten internen Recovery-Testinstallers,
werden mit wiederholtem `--artifact` gebunden. Die erlaubten Szenarien werden mit wiederholtem `--scope`
registriert. Das Feld `status` des Completion-Aufrufs ist eine Klassifikation des Controllers, kein aus
beliebiger Textausgabe automatisch abgeleiteter Produktnachweis. Ein unklassifizierter Skriptfehler ergibt
`INCONCLUSIVE`; er darf nicht pauschal als Produktfehler oder erfolgreicher Test verbucht werden.

Für vorhandene CI-Pakettestskripte fasst `run-ci` Ausgabe, Guard, Prozessaufruf und Abschluss zusammen:

```text
python scripts/acceptance_context.py run-ci --root EVIDENCE --controller CONTROLLER --action desktop-test --artifact INSTALLER --script scripts/test_windows_package.ps1 --evidence RAW_LOG -- pwsh -NoProfile -File scripts/test_windows_package.ps1 -ConfirmIsolatedEnvironment
```

`--script` ist wiederholbar und bindet die direkten Skriptbytes zusätzlich zu `--artifact`. Jeder genannte
Skriptpfad muss als separates Argument im Befehl vorkommen. Das vollständige argv wird im Kontext bewahrt.
Es gibt keine Shellinterpolation. `run-ci` erstellt einen autonomen 30-Minuten-Kontext und ruft vor dem
Prozessstart den CI-Guard auf; bei Guardfehler beginnt kein Subprozess. Die stdout-/stderr-Bytes werden ohne
Textumwandlung zwischen ausdrücklich gekennzeichneten JSON-Harness-Start-/Endereignissen aufgezeichnet.
Das Rohlog wird nicht überschrieben. Exitcode 0 erzeugt `PASS`, jeder andere Exitcode `INCONCLUSIVE`; der
Prozessexitcode wird weitergegeben. Ein technischer Startfehler erzeugt Exitcode 1 und `INCONCLUSIVE`.
Ein nichtleeres Log entsteht auch bei stillen Programmen aus den Harnessereignissen. Der übergeordnete
Controller klassifiziert Fehler anhand dieser Evidence; er darf sie nicht automatisch erneut ausführen.
Signierte Testläufe behalten die vorhandene `-RequireSignature`-Prüfung des gebundenen Pakettestskripts.

## VM-Binding, Benutzerpause und Vorprüfung

`init --binding FILE` und `guard --binding FILE` erwarten exakt diese Felder; das folgende Beispiel ist
synthetisch und vor Verwendung vollständig durch tatsächlich verifizierte Werte zu ersetzen:

```json
{
  "kind": "vm",
  "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "version": "0.0.0",
  "vm_uuid": "11111111-1111-4111-8111-111111111111",
  "snapshot_uuid": "22222222-2222-4222-8222-222222222222",
  "identity": "synthetic-operator",
  "os_version": "synthetic-windows"
}
```

Das erneute Binding stammt aus der aktuellen read-only Inventur. Eine gespeicherte alte Datei ersetzt keine
frische Zustandsprüfung. Snapshot-Lineage, Dienst-/Prozess-/Benutzerzustand, Zielpfade und Kollisionsfreiheit
werden vom vorhandenen technischen Harness geprüft und in dessen Rohbelegen dokumentiert.

Ein `issue --user-wait uac|pin|password|visual` erzeugt **exakt 120 Minuten** Gültigkeit. Eine zusätzliche
autonome Frist ist dabei verboten. Rein autonome Kontexte erfordern ausdrücklich `--autonomous-minutes N`
mit 1 bis 120 Minuten. Erzeugung, gespeicherte Frist und Guard werden gemeinsam validiert. Bei Ablauf,
Uhr-Rücksprung oder Bindungswiderspruch entsteht keine Autorisierung. Der Controller bewahrt Evidence,
klassifiziert den Versuch und beginnt gegebenenfalls neu vom gebundenen Ausgangssnapshot; Kontexte lassen
sich nicht verlängern oder zurücksetzen.

VM-Guards verlangen außerdem `--preflight FILE`. Diese vom technisch prüfenden Harness erstellte Attestation
enthält exakt:

```json
{
  "schema": "e-rechnungs-pruefer-acceptance-preflight/v1",
  "binding_sha256": "SHA256_DER_KANONISCHEN_BINDING_JSON_BYTES",
  "context_id": "ID_DES_AUSGESTELLTEN_KONTEXTS",
  "observed_at_utc": "2026-09-16T12:00:00.000000Z",
  "collision_free": true,
  "state_verified": true,
  "signatures_verified": false
}
```

Der Bindinghash verwendet das UTF-8-JSON ohne abschließenden Zeilenumbruch, mit sortierten Schlüsseln,
unmaskierten Unicodezeichen und `separators=(",", ":")`; die Pythonfunktion `digest(binding)` implementiert
dies exakt. Die Attestation muss an genau diesen Kontext gebunden sein, darf nicht aus der Zukunft stammen
und höchstens fünf Minuten alt sein. Ihr Hash wird im konsumierten Kontext erhalten. Die tatsächlichen
Prüfergebnisse dürfen nicht durch bloßes Erstellen einer JSON-Datei ersetzt werden.

`issue --require-signatures` verlangt eine frische Vorprüfung mit `signatures_verified=true` auch auf CI.
Dieser Wert wird erst nach erfolgreicher echter Prüfung aller gebundenen ausführbaren Artefakte gesetzt.
Die Vorprüfung ist ein Beleg des Controllers, keine vom Werkzeug selbst ausgeführte Authenticode-Prüfung.
Zugangsdaten, Tokenwerte und echte Rechnungsdaten gehören in keine dieser Dateien.

## Persistenz und Grenzen

Der festgelegte Controllername wird bei jeder Mutation geprüft; ein exklusiv angelegtes `.writer-lock`
verhindert gleichzeitige Schreiber. Ein nach Prozessabbruch stehen gebliebener Lock wird niemals automatisch
entfernt. Planänderungen werden atomar ersetzt und zuvor als vollständiger Zustand an eine hashverkettete
`events.ndjson` angehängt. Ein Abbruch zwischen beiden Schreibvorgängen sperrt die Wiederaufnahme wegen
widersprüchlicher Evidence. Frühere Events und terminale Receipts werden nicht überschrieben.

Die Dateien schützen gegen versehentliche Verwechslung, veraltete Kontexte, konkurrierende Aufrufe und
erkannte Manipulation. Sie sind keine externe kryptografische Identitätsprüfung eines gleichberechtigten
lokalen Kontos und kein manipulationssicherer Ersatz für Host-/VM-Berechtigungen. Ein einmaliger Guard kann
das anschließende Shellskript nicht gegen absichtliches Umgehen absichern; Workflow und Controller müssen
ihn unmittelbar vor jeder gebundenen Aktion aufrufen und bei Fehler abbrechen. Zwischen Guard und Aktion
dürfen weder Artefakte noch Zielzustand ausgetauscht werden.

Nach einem Nicht-PASS existiert absichtlich kein `reset`, `retry`, Controllerwechsel oder Snapshotbefehl in
dieser CLI. Die in `ACCEPTANCE.md` vorgeschriebene Klassifikation, Versiegelung, unabhängige Harnessprüfung
und maximal zwei fehlgeschlagene automatisierte Versuche werden vom übergeordneten Releaseplan geführt.
