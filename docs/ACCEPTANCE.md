# Organisation der Release-Abnahme

Dieses Dokument regelt die technische und manuelle Abnahme eines Release-Kandidaten. Ziel ist ein
fail-closed ausführbarer Lauf, der nach einer Unterbrechung allein aus Repository, maschinenlesbarem Zustand
und versiegelter Evidence fortgesetzt oder abschließend bewertet werden kann. Ein Chat darf Arbeit koordinieren,
ist aber weder Zustandsablage noch Nachweis.

Die fachliche Testmatrix und die Veröffentlichungsreihenfolge stehen in [`RELEASE.md`](RELEASE.md). Die
Sicherheits- und Übergaberegeln in [`../AGENTS.md`](../AGENTS.md) gelten zusätzlich.

## Ein schreibender Controller

Jeder Abnahmelauf hat genau einen benannten Controller, der folgende Zustände verändern darf:

- `acceptance-plan.json` und das append-only Ereignisprotokoll;
- Claims, Laufzeitkontexte und Autorisierungen;
- VM- und Produktzustand innerhalb des freigegebenen Scopes;
- Receipts, Screenshots, Rohprotokolle und Manifeste.

Weitere Agents oder Chats dürfen parallel analysieren und Evidence read-only prüfen. Sie dürfen weder einen
zweiten Lauf fortsetzen noch Claims konsumieren, Snapshots wiederherstellen oder Evidence ergänzen. Ein
Controllerwechsel benötigt ein versiegeltes Übergabereceipt; vorher bleibt der neue Controller read-only.

## Maßgeblicher Laufzustand

Vor der ersten Produkt- oder VM-Mutation erzeugt der Controller unter
`local-data/release-evidence/<version>/<commit>/acceptance-plan.json` einen gebundenen Plan. Die Datei bleibt
lokal und wird nicht in Git aufgenommen. Sie wird atomar ersetzt; jede Zustandsänderung wird zusätzlich in
`events.ndjson` angehängt. Als synthetischer, vor Verwendung vollständig zu ersetzender Ausgangspunkt dient
[`acceptance-templates/acceptance-plan.json`](acceptance-templates/acceptance-plan.json); sichtbare Phasen
verwenden entsprechend [`acceptance-templates/ui-task.json`](acceptance-templates/ui-task.json). Der Plan enthält
mindestens:

- Schema-Version, Run-ID sowie Erstellungs- und Änderungszeit in UTC;
- Version, vollständigen Commit-SHA und gegebenenfalls Tagobjekt;
- Workflow-Run und -Versuch, Artefakt-IDs, Dateinamen, Größen und SHA-256-Werte;
- Controlleridentität und erlaubten Aktionsscope;
- pro Umgebung VM-UUID, gebundene Snapshot-UUID, Betriebssystemstand und Ausgangszustand;
- pro Szenario Ausgangsstand, Prüfschritte, Modus, Versuchszähler, Status und autoritative Evidence-Pfade;
- auslösende beziehungsweise entfallene bedingte Tests mit Begründung;
- aktive Wartekontexte einschließlich Ablaufzeit und noch erforderlicher Benutzeraktion;
- offene Befunde, Klassifikation, Stopentscheidung und erlaubte nächste Aktion.

Pfade in Plan und Receipts sind nach Möglichkeit relativ zum Evidence-Stamm. Zugangsdaten, Tokeninhalte,
echte Rechnungsdaten und andere Geheimnisse dürfen weder im Plan noch in der getrackten Dokumentation stehen.

## Statusmodell

Nichtterminale Szenariostatus sind `PENDING`, `READY`, `RUNNING` und `WAITING_USER`. Jeder tatsächlich
begonnene Versuch endet dagegen mit genau einem der folgenden terminalen Status:

| Status | Bedeutung |
|---|---|
| `PASS` | Alle gebundenen Bestehenskriterien sind durch autoritative Evidence erfüllt. |
| `FAIL_PRODUCT` | Ein reproduzierbarer Produktfehler des gebundenen Kandidaten ist bestätigt. |
| `FAIL_HARNESS` | Harness oder Controller hat den vorgesehenen Produktnachweis verhindert. |
| `FAIL_ENVIRONMENT` | Gast, Host, VM, Uhr, Rechte oder Fremdsoftware hat den Nachweis verhindert. |
| `INCONCLUSIVE` | Die vorhandenen Beobachtungen erlauben keine belastbare Klassifikation oder Aussage. |
| `ABORTED` | Der Versuch wurde vor einem Ergebnis kontrolliert beendet; erfolgte Mutationen sind dokumentiert. |
| `SUPERSEDED` | Ein späterer, ausdrücklich referenzierter Versuch ersetzt diesen Versuch als Autorität. |

Ein Befund wird zuerst klassifiziert. Bis dahin bleiben Wiederholung, Cleanup und Snapshot-Restore gesperrt.
`FAIL_PRODUCT` beendet die weitere Abnahme dieses Kandidaten. Ein späteres `PASS` darf einen Produktfehler nur
für einen neuen, vollständig gebundenen Kandidaten ersetzen, nicht durch Wiederholung auf denselben Bytes.

## Ausführungsschichten

Deterministische Prüfungen bleiben in CLI, PowerShell und den vorhandenen Testskripten:

- Commit-, Tag-, Artefakt-, Hash- und Authenticode-Bindung;
- VM-/Snapshotinventar und technische Vor- und Nachbedingungen;
- Installation, Deinstallation, Dienst, Registry, Dateien, Listener und Healthcheck;
- API-, Schema-, PDF/XML-, KoSIT- und Paketprüfungen;
- Evidence-Erzeugung, Manifestierung und Verifikation.

Computer Use wird nur eingesetzt, wenn ein sichtbarer GUI-Ablauf über Dateien und Kommandoausgaben nicht
überzeugend nachweisbar ist, insbesondere für Browsercache, Alt-Tab, Öffnen-Client, sichtbare Meldungen und
Darstellungsfehler. Der Auftrag muss Startzustand, erlaubte Klicks und Eingaben, erwartete Beobachtungen,
Screenshot-Checkpoints und Stopbedingungen vorab im Plan festlegen. Computer Use:

- entscheidet keine Hash-, Signatur-, VM- oder Dateibindung;
- schreibt Evidence nicht außerhalb des Controllers;
- umgeht keine UAC-, PIN-, Passwort- oder Berechtigungsabfrage;
- stoppt bei unerwartetem Ziel, Fokusverlust, abgelaufenem Kontext oder nicht vorgesehenem Dialog;
- meldet Beobachtung und Screenshot zurück; der Controller klassifiziert das Ergebnis.

Der MCP-Server und die Computer-Use-Fähigkeit müssen vor dem Lauf eingerichtet und geprüft sein. Ihre
Einrichtung während eines bereits mutierenden Abnahmelaufs ist kein zulässiger Reparaturpfad.

## Benutzeraktion und 120-Minuten-Kontext

Nur UAC-Bestätigung, PIN-/Passworteingabe oder eine echte manuelle Sichtprüfung werden an den Nutzer gegeben.
Der Controller fragt niemals nach Zugangsdaten und zeichnet sie nicht auf. Ein darauf wartender Kontext trägt
eine UTC-Erzeugungszeit und eine harte Ablaufzeit von 120 Minuten; Generator, Laufzeitprüfung und Tests müssen
diese Frist durchsetzen.

Nach der Rückkehr werden vor jeder weiteren Mutation erneut geprüft:

1. Lauf-, Kandidaten- und Controlleridentität;
2. VM-UUID und Snapshot-Lineage;
3. Artefaktname, Größe, SHA-256 und erforderliche Signatur;
4. Produkt-, Prozess-, Dienst- und Kollisionszustand;
5. Gültigkeit und Einmaligkeit von Claim und Kontext.

Ein abgelaufener oder widersprüchlicher Kontext wird nicht verlängert oder rekonstruiert. Der Versuch endet
fail-closed und wird nach Klassifikation vom gebundenen Ausgangssnapshot neu begonnen.

## Wiederholung und Snapshot-Neustart

Nach einem klassifizierten Harness- oder Umgebungsfehler wird die vorhandene Evidence zunächst versiegelt. Erst
danach darf der gebundene saubere Snapshot wiederhergestellt werden. Jeder neue Versuch erhält eine neue
Attempt-ID, neue Claims und neue Evidence; frühere Bytes bleiben unverändert.

Pro Szenario sind höchstens zwei fehlgeschlagene automatisierte Versuche mit `FAIL_HARNESS` oder
`FAIL_ENVIRONMENT` zulässig. Danach wird kein weiteres Ad-hoc-Harness gebaut. Das Szenario wechselt auf den im
Plan vorab festgelegten Computer-Use- oder manuellen Pfad oder endet `INCONCLUSIVE`. Ein sicher behebbarer
Harnessfehler darf innerhalb dieses Budgets eine getrennte, versionierte Revision erhalten. Automatische
Wiederholungen bei `FAIL_PRODUCT`, unbekanntem Zustand oder fehlender Bindung sind verboten.

Live-State-Fortsetzungen sind Ausnahmen. Sie benötigen eine eigene Autorisierung, eine frische
Zustandsattestation und eine Begründung, warum Snapshot-Neustart den benötigten Nachweis zerstören würde.

## Evidence und Aufbewahrung

Die verbindlichen Retention-Klassen, Mindestfristen und Archiv-Gates stehen in
[`EVIDENCE_RETENTION.md`](EVIDENCE_RETENTION.md).

Jeder Versuch besitzt ein eigenes Verzeichnis mit mindestens:

- `result.json` mit Status, Bindungen, Zeitstempeln und autoritativen Nachweisen;
- unveränderter Roh-Ausgabe von Host, Gast und Werkzeugen;
- den vereinbarten Screenshots beziehungsweise Operatorattestationen;
- `SHA256SUMS.txt` für alle autoritativen Dateien;
- bei Mutation einen belegten Vor- und Nachzustand.

Originalartefakte, Abschlussreceipts, Manifeste, Produktfehler und nicht reproduzierbare Beobachtungen sind
kanonische Evidence. Entpackte Kopien und andere aus einem behaltenen Original bytegenau reproduzierbare Daten
werden als `DERIVED` gekennzeichnet. Harnessrevisionen und abgebrochene Versuche bleiben bis zum Closeout
unverändert und erhalten dann ausdrücklich `ABORTED` oder `SUPERSEDED`; sie werden nicht stillschweigend
umgedeutet.

Der Closeout erzeugt einen lokalen `INDEX.json`, der Authority-, Retention- und Ableitungsklassen festhält,
sowie ein finales Inventar, das jeden Verzeichnis- und Dateistand mit Modus, Größe und SHA-256 bindet. Eine
spätere Deduplizierung oder Löschung benötigt ein Pre-Cleanup-Inventar, eine explizite Löschliste und ein
Post-Cleanup-Receipt. Private Evidence wird nicht in Git oder öffentliche Releaseassets aufgenommen.

## Unterbrechung, Wiederaufnahme und Abschluss

Ein neuer Chat oder Controller liest in dieser Reihenfolge:

1. `AGENTS.md` und dieses Dokument;
2. `docs/RELEASE.md` und den Plan des konkreten Releases;
3. `acceptance-plan.json`, `events.ndjson` und die dort referenzierten terminalen Receipts;
4. bei einem abgeschlossenen Release dessen Closeout unter `docs/release-history/` und den lokalen
   Evidence-Index.

Vor einer Wiederaufnahme werden Plan, Manifeste, aktive Claims, Ablaufzeiten sowie VM- und Artefaktbindungen
read-only verifiziert. Fehlt eine dieser Grundlagen, wird nicht aus dem Chat rekonstruiert: Der Lauf endet
`INCONCLUSIVE` oder beginnt nach Versiegelung der vorhandenen Evidence als neuer Versuch vom sauberen Snapshot.

Ein Release ist abgeschlossen, wenn alle Pflichtszenarien terminal bewertet, offene Befunde klassifiziert,
Publikationsstatus und Assetinventar dokumentiert, alle Kontexte abgelaufen beziehungsweise geschlossen und
`INDEX.json` sowie finales Manifest verifiziert sind. Der getrackte Closeout nennt nur öffentliche, unkritische
Fakten. Er verändert weder Tag noch veröffentlichte Assets.
