# Aufbewahrung von Release-Evidence

Diese Richtlinie ergänzt [`ACCEPTANCE.md`](ACCEPTANCE.md). Sie legt fest, welche Release-Nachweise lokal,
öffentlich oder in einem verschlüsselten Archiv aufbewahrt werden und wann reproduzierbare Daten entfernt
werden dürfen. Ein Chat ist weder Retention-Entscheidung noch Archivnachweis.

## Klassen und Fristen

| Klasse | Aufbewahrung |
|---|---|
| `PUBLIC_CANONICAL` | Veröffentlichte Assets, Prüfsummen und Publikationsbelege dauerhaft behalten. |
| `PRIVATE_CANONICAL` | Autoritative private PASS-Evidence mindestens bis zum Supportende und dem Ende einer festgelegten Auditfrist behalten. |
| `RESTRICTED_BLOCKER` | Produktfehler-Evidence verschlüsselt bis zum Supportende des betroffenen Releases plus zwölf Monate archivieren; niemals veröffentlichen. |
| `HISTORICAL_SUPERSEDED` | Kleine Receipts, Manifeste und Hashes dauerhaft behalten; große Binärdaten frühestens 90 Tage nach Veröffentlichung entfernen. |
| `DERIVED_REPRODUCIBLE` | Nach bytegenau geprüfter Ableitungskarte und versiegelter Vorher-Inventur ohne weitere Wartefrist entfernbar. |
| `CONTAMINATION` | Nur mit exakter Löschliste, Vorher-Inventur und anschließendem Cleanup-Receipt entfernen. |

Die Fristen sind Untergrenzen. Vertragliche, steuerliche, sicherheitsbezogene oder andere verbindliche
Anforderungen gehen vor.

## Gate für verschlüsselte Archive

Kanonische oder nicht reproduzierbare Evidence darf erst aus dem Arbeitsbestand entfernt werden, wenn:

1. ein vom Arbeitsvolume unabhängiges Ziel mit ausreichendem Speicher gebunden ist;
2. ein zeitgemäß verschlüsselter Container zunächst unter einem temporären Namen erzeugt wurde;
3. der Recovery-Schlüssel ausschließlich vom Nutzer verwahrt und außerhalb des Arbeitsrechners gesichert ist;
4. Ciphertext-Größe und SHA-256 festgehalten wurden;
5. der Container schreibgeschützt geöffnet und jeder erwartete Klartext-Hash erneut geprüft wurde;
6. ein Cloud- oder externes Ziel die vollständige Übernahme bestätigt hat;
7. ein Locator und ein versioniertes Archivreceipt versiegelt wurden.

Kennwörter, private Schlüssel und Recovery-Codes stehen niemals in Git, Evidence, Receipts, Logs oder Chats.
Ein Upload ohne verifizierten Readback oder ein lokales Time-Machine-Snapshot gilt nicht als Cold Archive.
Gelangt ein unverschlüsseltes Zwischenartefakt in einen Cloud- oder File-Provider-Pfad, genügt lokales Löschen
nicht. Der Lauf bleibt gesperrt, bis Deleted-File- und Versionshistorie geprüft und eine vorhandene Kopie zur
permanenten Löschung markiert oder das verbleibende Risiko ausdrücklich klassifiziert ist.

## Reproduzierbare Ableitungen

Eine Ableitungskarte bindet für jede zu entfernende Datei mindestens Zielpfad, Größe, SHA-256, kanonisches
Quellarchiv und dessen SHA-256 sowie den eindeutigen Memberpfad. Bei verschachtelten Archiven werden beide
Memberstufen gebunden. Vor der Löschung wird in einem temporären Verzeichnis rekonstruiert und bytegenau
verglichen. Die kanonischen Quellarchive bleiben erhalten.

Historische Receipts dürfen anschließend auf nicht mehr materialisierte Pfade verweisen. Sie werden nicht
umgeschrieben; das neue Retention-Receipt verweist stattdessen auf die Ableitungskarte und beschreibt den
Restoreweg.

## Versionierte Versiegelung

Ein Retention-Lauf überschreibt keinen früheren Closeout. Er erzeugt eigene Pre-/Post-Inventare, Plan,
Ableitungs- oder Archivkarte, Receipt und detached Prüfsummen außerhalb der inventarisierten Evidence-Roots.
Ein künftiger Task liest zuerst den getrackten Release-Closeout und anschließend den dort genannten neuesten
lokalen Retention-Beleg.
