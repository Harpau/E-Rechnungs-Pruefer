# Fachlicher Vertrag für Automatisierungsintegrationen

## Zweck und Verbindlichkeit

Dieses Dokument legt die fachlichen Status- und Fehlerregeln für unbeaufsichtigte Integrationen fest, zunächst
für den geplanten Node-RED-Mailflow. Es ist die verbindliche Grundlage für die nachfolgenden API-, Windows- und
Flow-Erweiterungen.

Die Schlüsselwörter **MUSS**, **DARF NICHT**, **SOLL** und **DARF** sind normativ. Noch nicht implementierte
Transportdetails sind als Zielvertrag gekennzeichnet und dürfen erst nach Tests als verfügbar dokumentiert
werden.

## Getrennte Statusachsen

Eine Integration MUSS Erkennung, fachliche Prüfung und offizielle Prüfung getrennt behandeln. Kein Status darf
aus einem anderen abgeleitet werden, sofern die folgende Tabelle dies nicht ausdrücklich vorsieht.

### 1. Dokumenterkennung

| Wert | Bedeutung | Automatisierungsentscheidung |
|---|---|---|
| `CII` | CII/UN CEFACT CrossIndustryInvoice erkannt | unterstützte E-Rechnung |
| `UBL` | UBL Invoice oder CreditNote erkannt | unterstützte E-Rechnung |
| `UNKNOWN` | XML ist technisch lesbar, aber keine unterstützte Rechnungssyntax | keine unterstützte E-Rechnung |
| kein Analyseergebnis | Eingabe konnte nicht sicher als XML beziehungsweise Hybrid-PDF verarbeitet werden | gemäß Eingabe- oder Betriebsfehler behandeln |

Eine Rechnung mit Syntax `CII` oder `UBL` bleibt auch bei Prüfstatus `invalid` eine erkannte E-Rechnung. Sie DARF
NICHT als „keine E-Rechnung“ bezeichnet werden. `UNKNOWN` bedeutet nicht, dass das Dokument rechtlich niemals
eine E-Rechnung sein kann; es bedeutet ausschließlich, dass diese Anwendung die Syntax nicht unterstützt.

Eine reine Sicht- oder Scan-PDF ohne eingebettete strukturierte XML ist keine verarbeitbare E-Rechnung. Eine
OCR-Rekonstruktion DARF NICHT stattfinden.

### 2. Gemeinsamer Prüfstatus

| Wert | Bildung | Bedeutung |
|---|---|---|
| `ok` | keine Fehler und keine Warnungen | im ausgeführten Prüfumfang ohne Befund |
| `warning` | keine Fehler, mindestens eine Warnung | erkannt und verarbeitbar, aber mit prüfungsbedürftigem Hinweis |
| `invalid` | mindestens ein Fehler oder eine ausgeführte KoSIT-Ablehnung | erkannt, aber im ausgeführten Prüfumfang nicht konform |

Hinweise der Severity `info` verändern den Prüfstatus nicht. `ok` ist weder eine Steuer- oder Rechtsberatung noch
eine Garantie der Echtheit, vollständigen Profilkonformität oder Zahlungsberechtigung.

Für jede erkannte E-Rechnung MUSS ein lesbarer Bericht erzeugt werden, auch bei `warning` oder `invalid`.

### 3. Offizieller KoSIT-Status

| Wert | Technische Grundlage | Bedeutung |
|---|---|---|
| `accepted` | KoSIT ausgeführt, valider Bericht, maßgebliche Annahmeentscheidung | im verwendeten KoSIT-Szenario angenommen |
| `rejected` | KoSIT ausgeführt, valider Bericht, maßgebliche Ablehnungsentscheidung | im verwendeten KoSIT-Szenario abgelehnt |
| `not-requested` | Aufruf hat die offizielle Prüfung deaktiviert | keine offizielle Aussage angefordert |
| `unavailable` | Prüfung angefordert, KoSIT aber nicht konfiguriert oder deaktiviert | keine offizielle Aussage verfügbar |
| `indeterminate` | Prüfung angefordert und gestartet oder startbereit, aber Timeout, Startfehler oder kein valider Bericht | technische Störung; keine Rechnungsentscheidung |

`accepted` und `rejected` dürfen nur aus einem auswertbaren KoSIT-Bericht beziehungsweise dem bereits
dokumentierten Kompatibilitätsrückfall gebildet werden. Eine vorhandene `<rep:accept/>`- oder
`<rep:reject/>`-Entscheidung ist gegenüber dem Prozessrückgabecode maßgeblich.

`not-requested`, `unavailable` und `indeterminate` sind niemals Ablehnungen. `indeterminate` MUSS zusätzlich als
technische Warnung im Bericht erscheinen. Ob der gesamte Mailflow deshalb wiederholt wird, richtet sich nach der
für den Prozess konfigurierten Pflicht zur offiziellen Prüfung:

- Ist KoSIT für den Geschäftsprozess verpflichtend, MUSS `unavailable` oder `indeterminate` als nicht
  abgeschlossene Verarbeitung behandelt werden.
- Ist KoSIT optional, DARF der Bericht mit deutlich ausgewiesenem KoSIT-Status versendet werden.

Für den produktiven Rechnungseingang SOLL KoSIT verpflichtend konfiguriert werden. Eine bewusste Abweichung MUSS
in der Node-RED-Konfiguration sichtbar dokumentiert sein.

## Transportvertrag für HTML- und PDF-Berichte

`POST /api/report` liefert den eigenständigen HTML-Bericht für Browser und bestehende Integrationen.
`POST /api/report/pdf` liefert denselben fachlichen Bericht als direkt öffnungsfähigen PDF-Anhang für
Mail-Automatisierungen. Beide Endpunkte verwenden dieselben maschinenlesbaren ASCII-Header:

| Header | Zulässige Werte | Zweck |
|---|---|---|
| `X-Einvoice-Syntax` | `CII`, `UBL`, `UNKNOWN` | Dokumenterkennung |
| `X-Einvoice-Validation-Status` | `ok`, `warning`, `invalid` | gemeinsamer Prüfstatus |
| `X-Einvoice-Official-Status` | `accepted`, `rejected`, `not-requested`, `unavailable`, `indeterminate` | KoSIT-Status |

Die Header sind die maschinenlesbare Zusammenfassung. HTML- und PDF-Inhalt bleiben menschenlesbare Berichte.
Rechnungsnummern, Originaldateinamen, Namen, Steuerkennungen oder andere fachliche Inhalte DÜRFEN NICHT in
zusätzliche Antwort-Header oder den Download-Dateinamen aufgenommen werden, weil Header häufiger in
Infrastrukturprotokollen landen. Die gelieferten Namen lauten deshalb fest
`E-Rechnungs-Pruefbericht.html` beziehungsweise `E-Rechnungs-Pruefbericht.pdf`.

Der PDF-Bericht begrenzt zum Schutz vor unkontrolliert großen Mailanhängen die Darstellung auf höchstens 250
Rechnungspositionen, 250 Findings und 50 Rechnungshinweise. Einzelwerte, Gesamttext und Zeilenumbrüche sowie
technische XML-Zeilen, Roh-XML- und KoSIT-Ausschnitte besitzen zusätzliche feste Budgets. Der Bericht weist jede
Kürzung und bei Positionen sowie Findings die dargestellte und ursprüngliche Anzahl sichtbar aus. Würde das
Ergebnis dennoch mehr als 200 Seiten benötigen, liefert der Endpunkt einen kompakten, gültigen Ersatzbericht.
Die vollständigen analysierten Daten bleiben im HTML-Bericht und über `POST /api/analyze` zugänglich. Die
ursprüngliche Rechnungsdatei bleibt im Mailflow unverändert erhalten; das vollständige ausgewählte XML kann
außerdem byteidentisch über `POST /api/xml` exportiert werden.

## HTTP- und Betriebsfehler

HTTP-Statuscodes beschreiben den Transport beziehungsweise die technische Verarbeitbarkeit, nicht die fachliche
Gültigkeit einer erkannten Rechnung. Insbesondere MUSS eine erkannte E-Rechnung mit Prüfstatus `invalid` weiterhin
einen erfolgreichen Berichts-Response erhalten.

| Ergebnis | Fehlerklasse | Behandlung im Flow |
|---|---|---|
| `2xx` mit Syntax `CII` oder `UBL` | erfolgreich erkannte E-Rechnung | Bericht gemäß Prüf- und KoSIT-Status verarbeiten |
| `2xx` mit Syntax `UNKNOWN` | terminaler Kandidatenfehler | Kandidat ist keine unterstützte E-Rechnung; weitere Kandidaten prüfen |
| `413` oder `422` | terminaler Kandidatenfehler | Kandidat unzulässig, zu groß, unlesbar, unsicher oder ohne Rechnungs-XML; weitere Kandidaten prüfen |
| `400`, `404` oder `405` | Integrations- oder Konfigurationsfehler | nicht automatisch wiederholen; in technischen Fehlerpfad geben |
| `401` oder `403` | Authentifizierungs- oder Berechtigungsfehler | nicht automatisch wiederholen; Zugangskonfiguration korrigieren |
| `408`, `429` oder `5xx` | vorübergehender Betriebsfehler | begrenzt wiederholen |
| Verbindungsfehler oder Client-Timeout | vorübergehender Betriebsfehler | begrenzt wiederholen |
| unerwartete Antwort, fehlende Pflicht-Header oder nicht lesbarer Bericht | Protokollfehler | nicht als Rechnungsergebnis werten; technischen Fehlerpfad verwenden |

Bei einem zukünftigen API-Fehlerformat SOLL `detail` eine deutsche, für Menschen geeignete Beschreibung und
`type` einen stabilen maschinenlesbaren Fehlercode enthalten. Der Flow DARF fachliche Entscheidungen nicht durch
Textsuche in `detail` treffen.

## Auswahl und Behandlung von Mailanhängen

1. Der Flow MUSS alle XML- und PDF-Kandidaten erfassen. Dateiendung und MIME-Typ dienen nur zur Vorauswahl und
   sind nicht vertrauenswürdig.
2. XML-Kandidaten SOLLEN vor PDF-Kandidaten geprüft werden. Dadurch verdeckt eine gewöhnliche Sicht-PDF keine
   separat beigefügte Rechnungs-XML.
3. Innerhalb derselben Kandidatenklasse SOLL die ursprüngliche Reihenfolge erhalten bleiben.
4. Byteidentische Anhänge SOLLEN nur einmal geprüft werden.
5. Ein terminaler Kandidatenfehler beendet ausschließlich die Prüfung dieses Anhangs. Weitere Kandidaten MÜSSEN
   geprüft werden.
6. Mehrere unterschiedliche erkannte E-Rechnungen DÜRFEN NICHT stillschweigend auf die erste reduziert werden.
   Für jede erkannte Rechnung MUSS ein Ergebnis erzeugt oder die Mail in einen ausdrücklich ausgewiesenen
   Mehrdeutigkeits-/Fehlerpfad übergeben werden.
7. Ein vorübergehender Betriebs- oder Protokollfehler lässt den betroffenen Kandidaten offen. Die Mail DARF dann
   nicht als vollständig verarbeitet gelten.

Eine PDF, aus der der Prüfer sicher eine Rechnungs-XML extrahiert, wird nach der erkannten XML-Syntax bewertet.
Der PDF-MIME-Typ allein ist keine E-Rechnungsentscheidung.

## Mailergebnis und Quittierung

Eine Eingangsmail befindet sich fachlich in genau einem der folgenden Abschlusszustände:

| Abschlusszustand | Voraussetzung |
|---|---|
| `processed` | alle Kandidaten terminal behandelt, alle erkannten E-Rechnungen berichtet und alle erforderlichen Ausgaben erfolgreich versendet |
| `not-supported` | alle Kandidaten terminal behandelt und keine unterstützte E-Rechnung erkannt |
| `manual-review` | fachliche Mehrdeutigkeit oder dauerhaft nicht automatisch lösbarer Integrationsfehler wurde erfolgreich in einen dauerhaften Fehlerpfad übergeben |
| nicht abgeschlossen | mindestens ein Kandidat, Prüfschritt oder Versand ist noch offen beziehungsweise nur vorübergehend fehlgeschlagen |

Die IMAP-Mail DARF erst quittiert werden, wenn `processed`, `not-supported` oder `manual-review` erreicht ist.
Dabei gilt:

- Ein Berichtversand MUSS erfolgreich abgeschlossen sein, bevor die zugehörige Eingangsmail quittiert wird.
- Ein SMTP-Fehler ist ein vorübergehender Betriebsfehler und DARF nicht zum Quittieren führen.
- Ein technischer API-Fehler DARF nicht als `not-supported` umgedeutet werden.
- `manual-review` ist nur erreicht, wenn die Übergabe an einen dauerhaften Fehlerkanal nachweislich erfolgreich
  war. Ohne einen solchen Kanal bleibt die Mail nicht abgeschlossen.
- Die Quittierung SOLL idempotent sein. Wiederholungen dürfen nicht zu unkontrollierten mehrfachen Berichten
  führen.

## Wiederholungen und technischer Fehlerpfad

Vorübergehende Betriebsfehler MÜSSEN begrenzt und mit wachsender Wartezeit wiederholt werden. Als Standard für
den Node-RED-Flow gelten drei Wiederholungen nach ungefähr 30 Sekunden, 2 Minuten und 10 Minuten. Ein Serverhinweis
wie `Retry-After` SOLL Vorrang haben. Der Beispielflow akzeptiert dafür Sekunden oder ein HTTP-Datum und begrenzt
auch den Serverwert auf höchstens zehn Minuten.

Nach ausgeschöpften Wiederholungen MUSS die Mail in einen dauerhaften, überwachten Fehlerpfad übergeben werden,
zum Beispiel einen IMAP-Fehlerordner oder eine persistente Dead-Letter-Queue. Zugangsdaten-, Konfigurations- und
Protokollfehler gehen ohne wirkungslose automatische Wiederholungen direkt in diesen Pfad.

Der Fehlerpfad MUSS mindestens Zeitpunkt, Fehlerklasse, betroffenen Anhang und Korrelationskennung enthalten. Er
DARF weder Rechnungsbytes noch sensible Rechnungsinhalte in gewöhnliche Anwendungsprotokolle schreiben.
Der Beispielflow bezeichnet den maschinenlesbaren ISO-8601-Zeitpunkt als `automationError.occurredAt`.

Überschreitet eine Eingangsnachricht die konfigurierte 64-MiB-Grenze, liefert der IMAP-Knoten
`IMAP_EMAIL_MESSAGE_TOO_LARGE`. Dieser Fall MUSS im dauerhaften Fehlerkanal zur manuellen Bearbeitung sichtbar
werden und DARF nicht automatisch quittiert werden. Bis ein Bediener die Nachricht im Postfach bewusst behandelt,
ist ihre spätere erneute Zustellung erwartetes At-least-once-Verhalten; daraus entsteht kein zusätzlicher
automatischer ACK-Pfad.

## Datenschutz und Sicherheitsgrenzen

- Rechnungsdateien DÜRFEN nur an den explizit konfigurierten lokalen Prüfdienst gesendet werden.
- Der Dienst MUSS auf dem Windows-Rechner ausschließlich an Loopback gebunden bleiben, solange kein eigenes
  Netzwerk-Sicherheitskonzept umgesetzt ist.
- HTTP-Weiterleitungen MÜSSEN für den API-Aufruf deaktiviert sein. Der Beispielflow MUSS für diesen Request eine
  eigene Proxy-Konfiguration verwenden, die Prozess-Proxyvariablen überschreibt, `127.0.0.1` und `localhost`
  ausnimmt und nur eine lokale Sentinel-Adresse enthält. Der unmittelbar vorgeschaltete URL-Guard MUSS Schema,
  exakten Host, Port und Pfad prüfen, sodass ausschließlich eine der beiden Loopback-Adressen den HTTP-Knoten
  erreicht.
- Node-RED MUSS das persistente API-Token als `Authorization: Bearer <Token>` senden. Der Wert MUSS aus dem
  geschützten Node-RED-Credential-Speicher oder einer Prozessumgebung stammen.
- Zugangstoken DÜRFEN NICHT im exportierten Node-RED-Flow, in URLs oder normalen Logs stehen.
- Originalanhänge und XML-Bytes DÜRFEN durch die Prüfung nicht verändert werden.
- Temporäre Verarbeitungsdaten MÜSSEN nach Abschluss des jeweiligen Aufrufs entfernt werden.

## Abnahmeszenarien für die Folgeausbaustufen

Die spätere API- und Flow-Implementierung MUSS mindestens folgende anonymisierte Fälle automatisiert absichern:

1. gültige und fachlich fehlerhafte CII;
2. UBL Invoice und UBL CreditNote;
3. Hybrid-PDF mit eingebetteter Rechnungs-XML;
4. Sicht-PDF ohne XML neben einer gültigen separaten XML;
5. unbekannte, aber wohlgeformte XML-Syntax;
6. beschädigte XML, verbotene DTD/ENTITY und Größenüberschreitung;
7. mehrere unterschiedliche Rechnungsanhänge und byteidentische Duplikate;
8. KoSIT `accepted`, `rejected`, `not-requested`, `unavailable` und `indeterminate`;
9. API-Verbindungsfehler, Timeout, `401`/`403`, `422`, `429` und `5xx`;
10. SMTP-Fehler nach erfolgreicher Prüfung;
11. Wiederholung ohne unkontrollierten doppelten Bericht;
12. Quittierung ausschließlich nach einem definierten Abschlusszustand.
