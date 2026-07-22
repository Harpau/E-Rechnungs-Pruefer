# Node-RED-Integration

## Vorlage

Die importierbare, anonymisierte Vorlage liegt unter
[`docs/examples/node-red-e-rechnungs-pruefer-flow.json`](examples/node-red-e-rechnungs-pruefer-flow.json). Sie
ersetzt den bisherigen direkten Aufruf von `parse.php` durch einen authentifizierten Aufruf von
`POST /api/report/pdf` und hängt den gelieferten PDF-Bericht an die Ergebnismail. Der PDF-Anhang lässt sich in
Outlook und anderen Mailprogrammen direkt per Doppelklick im registrierten PDF-Programm öffnen; der für
HTML-Anhänge auf macOS problematische Umweg über einen temporären Safari-`file://`-Pfad entfällt.

Die Vorlage enthält bewusst keine echten Mailadressen, Servernamen, Zugangsdaten oder API-Token. Sie enthält den
IMAP-Eingang, den regelmäßigen Trigger, den SMTP-Ausgang und die Quittierung bereits in einem Flow. Der Flow ist
nach dem Import zunächst deaktiviert. Vor dem Aktivieren müssen die enthaltenen SMTP- und IMAP-Kontoknoten sowie
das voreingestellte Abrufintervall von zehn Sekunden geprüft werden.

IMAP-Eingang und IMAP-Quittierung müssen exakt denselben Node-RED-Config-Node `imap-email account` verwenden.
Gleiche Server- und Zugangsdaten in zwei getrennten Account-Knoten reichen nicht, weil das IMAP-Modul die interne
Account-ID im signierten ACK-Token bindet. Die Vorlage ist bereits entsprechend verdrahtet; beim Bearbeiten oder
Ersetzen des Kontoknotens muss diese gemeinsame Referenz erhalten bleiben.

## Erforderliche Umgebung

Die folgenden Werte müssen in der Prozessumgebung von Node-RED gesetzt werden:

| Variable | Erforderlich | Bedeutung |
|---|---|---|
| `EINVOICE_API_TOKEN` | ja | Inhalt von `%LOCALAPPDATA%\E-Rechnungs-Pruefer\api-token.txt` |
| `EINVOICE_RESULT_TO` | ja | Empfängeradresse der Ergebnismail |
| `EINVOICE_API_URL` | nein | Standard: `http://127.0.0.1:8080/api/report/pdf` |
| `EINVOICE_REQUIRE_KOSIT` | nein | Standard: `true`; `false` überspringt die KoSIT-Prüfung und verwendet nur die interne Prüfung |

Das API-Token darf nicht in den Flow kopiert oder als URL-Parameter übertragen werden und muss dem gemeinsamen
Vertrag `^[A-Za-z0-9_-]{32,}$` entsprechen. Nach einer Änderung der Prozessumgebung muss Node-RED neu gestartet
werden. Läuft Node-RED unter einem anderen Windows-Benutzer, muss das
Token kontrolliert in dessen geschützte Prozessumgebung beziehungsweise Credential-Konfiguration übernommen
werden; der andere Benutzer kann die Datei im ursprünglichen Benutzerprofil normalerweise nicht lesen.

Der HTTP-Request-Knoten verwendet die mitgelieferte Proxy-Konfiguration
**Nur lokale API – kein externer Proxy**. Sie überschreibt für genau diesen Request die Proxyvariablen des
Node-RED-Prozesses und nimmt `127.0.0.1` sowie `localhost` aus. Als zusätzliche ausfallsichere Vorgabe enthält
sie nur eine lokale Sentinel-Adresse. Entscheidend ist die Kombination mit der unmittelbar vorgeschalteten
Prüfung von Schema, exaktem Host, Port und Pfad sowie deaktivierten Weiterleitungen: Nur eine der beiden
Loopback-Adressen erreicht den HTTP-Knoten, ohne dass Rechnungsbytes oder das API-Token an einen externen Proxy
gelangen können. Diese Proxy-Konfiguration und ihre Referenz am HTTP-Knoten dürfen nicht entfernt oder geändert
werden.

## Ablauf

Der Flow:

1. ruft das konfigurierte IMAP-Postfach regelmäßig ab und berücksichtigt nur Nachrichten ohne `\Seen`;
2. lädt die Mailanhänge und sammelt alle XML- und PDF-Kandidaten, XML vor PDF;
3. verwirft byteidentische Anhänge;
4. sendet jeden Kandidaten sequenziell als `multipart/form-data` an `/api/report/pdf`;
5. wertet Syntax-, Prüf- und KoSIT-Header getrennt aus;
6. hängt für jede erkannte CII-/UBL-Rechnung einen PDF-Bericht mit einer korrelationsbezogen eindeutigen,
   ausschließlich technischen Dateibezeichnung an;
7. wiederholt `408`, `429`, `5xx`, Verbindungsfehler und bei verpflichtendem KoSIT ein `indeterminate` nach etwa
   30 Sekunden, 2 Minuten und 10 Minuten; `Retry-After` wird sowohl in Sekunden als auch als HTTP-Datum
   ausgewertet und auf höchstens 10 Minuten begrenzt;
8. quittiert die Eingangsmail erst nach erfolgreichem SMTP-Versand oder wenn alle Kandidaten terminal als nicht
   unterstützt beziehungsweise unlesbar klassifiziert wurden.

Eine erkannte Rechnung mit Prüfstatus `invalid` bleibt eine E-Rechnung und erhält ebenfalls einen Bericht.
`unavailable` oder `not-requested` bei verpflichtendem KoSIT führt in den technischen Fehlerpfad. Eine reine PDF
ohne eingebettete Rechnungs-XML wird nicht per OCR rekonstruiert.

Bei `EINVOICE_REQUIRE_KOSIT=false` sendet der Flow das API-Feld `official=false`. Die Anwendung startet dann für
diesen Aufruf keine KoSIT-Prüfung; der Antwortstatus `not-requested` ist in diesem Modus ein reguläres Ergebnis.

Der HTTP-Client erhält für jeden Versuch über `msg.requestTimeout` eine Zeitgrenze von 90 Sekunden. Die
Flow-Vorlage verlässt sich dabei bewusst nicht auf ein gleichnamiges, von aktuellen Node-RED-Versionen nicht
ausgewertetes Editor-Feld. HTTP-Weiterleitungen sind sowohl im Request-Knoten als auch pro Nachricht
deaktiviert; der temporäre Steuerwert wird nach jedem Versuch entfernt.

Am enthaltenen IMAP-Eingang müssen `Attachments` aktiviert und `Seen` auf „Only without flag“ belassen werden;
die Vorlage setzt dafür `includeAttachments=true` und `seenSelection=exclude`. Fehlt die Attachment-Eigenschaft
trotzdem vollständig, gilt dies als Konfigurationsfehler: Die Nachricht geht ohne Quittierung in den technischen
Fehlerpfad. Ein vorhandenes, aber leeres Attachment-Array bleibt dagegen der reguläre Abschluss
`not-supported`.

Der IMAP-Eingang begrenzt eine vollständige Nachricht auf 64 MiB (`67108864` Byte) und verarbeitet höchstens
fünf Nachrichten je Abruf beziehungsweise gleichzeitig. Das begrenzt den Speicherbedarf bei mehreren großen
Anhängen, ohne das fachliche Größenlimit des lokalen API-Aufrufs zu verändern.

## Technischer Fehlerpfad

Der Link-Ausgang **TECHNISCHER FEHLER – persistent anbinden** ist absichtlich noch unverbunden. Solange dort kein
erfolgreicher dauerhafter Fehlerpfad angeschlossen ist, verbleibt die Mail unquittiert. Vor einem produktiven
Einsatz muss der Ausgang beispielsweise mit einem überwachten IMAP-Fehlerordner oder einer persistenten
Dead-Letter-Queue verbunden werden. Erst nach erfolgreicher Übergabe darf dort ein IMAP-ACK folgen.

Der Debug-Knoten schreibt ausschließlich das bereinigte Objekt `msg.automationError`. Rechnungsbytes,
Authorization-Header und vollständige Nachrichten werden nicht protokolliert. Jede Fehlerdiagnose enthält mit
`occurredAt` einen ISO-8601-Zeitpunkt; bekannte technische Codes bleiben für die Diagnose erhalten.

`IMAP_EMAIL_MESSAGE_TOO_LARGE` ist ein dauerhafter manueller Fehlerfall: Der Anhang wurde wegen der 64-MiB-Grenze
nicht verarbeitet und darf nicht automatisch quittiert werden. Der persistente Fehlerpfad muss den Vorgang zur
manuellen Bearbeitung sichtbar machen. Ein Bediener muss die betroffene Nachricht anschließend im Postfach
bewusst behandeln, beispielsweise nach Prüfung verschieben oder als gesehen markieren. Solange dies nicht
geschieht, ist die erneute Zustellung nach Ablauf der IMAP-Sperrzeit beabsichtigt. Die Vorlage ergänzt hierfür
keinen automatischen ACK-Pfad.

Schlägt erst das IMAP-ACK nach erfolgreichem SMTP-Versand fehl, läuft auch dieser Fehler in den technischen
Ausgang. Wegen der At-least-once-Zustellung des IMAP-Knotens kann ein späterer erneuter Zustellversuch in diesem
Randfall einen zweiten Bericht erzeugen. Der dauerhafte Fehlerpfad sollte deshalb die Korrelationskennung
überwachen und solche Fälle vor einer automatischen Wiederholung zur manuellen Prüfung stellen.

## Prüfung nach dem Import

Vor der Umschaltung des produktiven Postfachs sollten mindestens die Abnahmeszenarien aus
[`AUTOMATION_INTEGRATION.md`](AUTOMATION_INTEGRATION.md) mit synthetischen Anhängen geprüft werden. Besonders
wichtig sind eine Sicht-PDF neben gültiger XML, mehrere Rechnungsanhänge, KoSIT-Ausfall, falsches API-Token,
SMTP-Ausfall und die korrekte Quittierung nach einem Retry.
