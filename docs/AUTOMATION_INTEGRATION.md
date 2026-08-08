# Fachlicher Vertrag für Automatisierungsintegrationen

## Zweck und Verbindlichkeit

Dieses Dokument legt den Schema-2-Vertrag für unbeaufsichtigte Integrationen fest, insbesondere für den
Node-RED-Mailflow. Es ist die verbindliche Grundlage für API-, Windows- und Flow-Integrationen.

Die Schlüsselwörter **MUSS**, **DARF NICHT**, **SOLL** und **DARF** sind normativ.

## Sofortiger Breaking Change auf Schema 2

Schema 2 gilt ohne Übergangszeit. Server und Consumer müssen in einem gemeinsamen Wartungsfenster aktualisiert
werden. Ein Consumer MUSS bei JSON-Antworten `schema_version == 2` und bei Berichtsantworten
`X-Einvoice-Analysis-Schema: 2` verlangen. Fehlt diese Kennzeichnung oder enthält sie einen anderen Wert, ist die
Antwort ein Protokollfehler. Ein Fallback, ein Legacy-Adapter oder das Ableiten fehlender Achsen ist unzulässig.

Das Migrationsmapping ist:

| Bisheriger Vertrag | Schema-2-Vertrag | Migrationsregel |
|---|---|---|
| `validation.status` / `X-Einvoice-Validation-Status` | kein einzelner Ersatzwert | Consumer auf die drei unabhängigen Achsen `assessment.official`, `assessment.internal` und `assessment.processing` umstellen |
| `validation.official` / `X-Einvoice-Official-Status` | `assessment.official` / `X-Einvoice-Conformity-Status` | `status` direkt aus der offiziellen Achse lesen |
| `validation.builtin` | `assessment.internal` / `X-Einvoice-Internal-Status` | Status, Findings und Zähler nur aus der internen Achse lesen |
| implizit aus Fehlertext oder fehlenden Daten abgeleiteter Verarbeitungszustand | `assessment.processing` / `X-Einvoice-Processing-Status` | technischen Abschluss ausschließlich aus der Verarbeitungsachse lesen |
| `finding.location` | `semantic_references`, `occurrence` und `xml_location` | fachliche Referenz und technische Fundstelle getrennt verarbeiten |

Die Umstellung MUSS atomar erfolgen. Insbesondere darf ein Consumer den früheren gemeinsamen Prüfstatus nicht
aus den neuen Achsen nachbauen, weil dabei offizielle Ablehnung, interne Findings und technische
Unvollständigkeit erneut vermischt würden.

## Dokumenterkennung

| Wert | Bedeutung | Automatisierungsentscheidung |
|---|---|---|
| `CII` | CII/UN CEFACT CrossIndustryInvoice erkannt | unterstützte E-Rechnung |
| `UBL` | UBL Invoice oder CreditNote erkannt | unterstützte E-Rechnung |
| `UNKNOWN` | XML ist technisch lesbar, aber keine unterstützte Rechnungssyntax | keine unterstützte E-Rechnung |
| kein Analyseergebnis | Eingabe konnte nicht sicher als XML beziehungsweise Hybrid-PDF verarbeitet werden | gemäß Eingabe- oder Betriebsfehler behandeln |

Eine Rechnung mit Syntax `CII` oder `UBL` bleibt auch bei `assessment.official.status == rejected` oder
`assessment.internal.status == errors` eine erkannte E-Rechnung. Sie DARF NICHT als „keine E-Rechnung“
bezeichnet werden. `UNKNOWN` bedeutet nicht, dass das Dokument rechtlich niemals eine E-Rechnung sein kann; es
bedeutet ausschließlich, dass diese Anwendung die Syntax nicht unterstützt.

Eine reine Sicht- oder Scan-PDF ohne eingebettete strukturierte XML ist keine verarbeitbare E-Rechnung. Eine
OCR-Rekonstruktion DARF NICHT stattfinden.

## Drei unabhängige Bewertungsachsen

Eine Integration MUSS offizielle Konformität, interne Hinweise und technischen Verarbeitungsabschluss getrennt
behandeln. Kein Status darf aus einem anderen abgeleitet oder zu einem gemeinsamen Status verdichtet werden.

### `assessment.official`

| Status | Bedeutung | Routing bei verpflichtender offizieller Prüfung | Routing bei optionaler Prüfung |
|---|---|---|
| `accepted` | offizieller Bericht nimmt die Rechnung an | Bericht | Bericht |
| `rejected` | offizieller Bericht lehnt die Rechnung ab | Bericht mit Status `rejected` | Bericht mit Status `rejected` |
| `not-requested` | offizielle Prüfung wurde nicht angefordert | Konfigurationsfehler | Bericht |
| `unsupported` | erkannter Fall wird von der offiziellen Prüfung nicht unterstützt | qualifizierter Bericht; weitere Kandidaten prüfen | Bericht; weitere Kandidaten prüfen |
| `unavailable` | offizielle Prüfung ist nicht verfügbar | Konfigurationsfehler, sofern `processing` nicht bereits `incomplete` ist | Bericht, sofern `processing` nicht `incomplete` ist |
| `indeterminate` | keine belastbare offizielle Entscheidung möglich | begrenzt wiederholen | Bericht, sofern `processing` nicht `incomplete` ist |

`accepted` und `rejected` dürfen nur aus einem auswertbaren offiziellen Bericht gebildet werden. Eine vorhandene
`<rep:accept/>`- oder `<rep:reject/>`-Entscheidung ist gegenüber dem Prozessrückgabecode maßgeblich.
`not-requested`, `unsupported`, `unavailable` und `indeterminate` sind keine Ablehnungen.
Insbesondere ist `unsupported` auch bei verpflichtender offizieller Prüfung ein abgeschlossenes,
profilabhängig qualifiziertes Ergebnis: Der Bericht MUSS versendet und die Verarbeitung weiterer Kandidaten
MUSS fortgesetzt werden. Daraus DARF weder `accepted` noch `rejected` abgeleitet werden. Die Behandlung von
`not-requested`, `unavailable` und `indeterminate` bleibt unverändert.

Für den produktiven Rechnungseingang SOLL KoSIT verpflichtend konfiguriert werden. Eine bewusste Abweichung MUSS
in der Node-RED-Konfiguration sichtbar dokumentiert sein.

### `assessment.internal`

| Status | Bedeutung | Routing |
|---|---|---|
| `clear` | interne Prüfung ausgeführt, keine Warnungen oder Fehler | Bericht |
| `attention` | interne Prüfung ausgeführt, mindestens ein prüfungsbedürftiger Hinweis | Bericht mit Status `attention` |
| `errors` | interne Prüfung ausgeführt, mindestens ein Fehler | Bericht mit Status `errors` |
| `not-run` | interne Prüfung nicht ausgeführt | bei `processing=limited` Bericht; bei `processing=incomplete` Wiederholung; bei `processing=complete` Protokollfehler |

Ein interner Fehler ist weder automatisch eine offizielle Ablehnung noch ein Transportfehler. `clear` ist keine
Steuer- oder Rechtsberatung und keine Garantie für Echtheit, vollständige Profilkonformität oder
Zahlungsberechtigung.

### `assessment.processing`

| Status | Bedeutung | Routing |
|---|---|---|
| `complete` | alle vorgesehenen Verarbeitungsschritte abgeschlossen | Bericht, vorbehaltlich der Invariante zu `internal=not-run` |
| `limited` | Analyse abgeschlossen, aber technische Darstellung oder Prüfumfang begrenzt | Bericht mit sichtbarem Status `limited` |
| `incomplete` | vorgesehene Verarbeitung nicht abgeschlossen | begrenzt wiederholen; kein Bericht als abgeschlossen werten |

Für eine unterstützte Syntax ist `processing=incomplete` vor den anderen Bewertungsachsen maßgeblich. Dadurch
wird beispielsweise `official=unavailable` zusammen mit `processing=incomplete` wiederholt und nicht als
abgeschlossener Bericht versendet. `internal=not-run` ist bei `processing=incomplete` erwartbar. Dagegen ist
`internal=not-run` zusammen mit `processing=complete` ein inkonsistenter Schema-2-Zustand und MUSS als
Protokollfehler behandelt werden. `UNKNOWN` bleibt ein terminal nicht unterstützter Kandidat.

## Fachliche Referenz und technische Fundstelle

`BG-16` bezeichnet die fachliche EN-16931-Gruppe „Zahlungsanweisungen“. Es ist eine
`semantic_references`-Kennung und ausdrücklich kein Ort im Bericht, kein JSON-Pfad und kein XML-Pfad. Ein
Consumer MUSS für eine maschinenlesbare Fundstelle `occurrence.json_pointer` und für die konkrete XML-Quelle
`xml_location.path` beziehungsweise `xml_location.line` verwenden. Fehlen diese Angaben, darf aus `BG-16` keine
technische Position konstruiert werden.

## Transportvertrag für HTML- und PDF-Berichte

`POST /api/report` liefert den eigenständigen HTML-Bericht für Browser und bestehende Integrationen.
`POST /api/report/pdf` liefert denselben fachlichen Bericht als direkt öffnungsfähigen PDF-Anhang für
Mail-Automatisierungen. Beide Endpunkte akzeptieren als Multipart-Form-Feld `scope=readable|complete`.
Der Standard `readable` enthält die Rechnungsdarstellung und menschenlesbare Prüfergebnisse. `complete`
ergänzt technische XML-Felder, XML-Darstellungen und KoSIT-Rohdaten. Ein anderer Wert wird mit `422`
abgewiesen. Beide Endpunkte verwenden dieselben maschinenlesbaren ASCII-Header:

| Header | Zulässige Werte | Zweck |
|---|---|---|
| `X-Einvoice-Analysis-Schema` | exakt `2` | Version des harten Analysevertrags |
| `X-Einvoice-Syntax` | `CII`, `UBL`, `UNKNOWN` | Dokumenterkennung |
| `X-Einvoice-Conformity-Status` | `accepted`, `rejected`, `not-requested`, `unsupported`, `unavailable`, `indeterminate` | `assessment.official.status` |
| `X-Einvoice-Internal-Status` | `clear`, `attention`, `errors`, `not-run` | `assessment.internal.status` |
| `X-Einvoice-Processing-Status` | `complete`, `limited`, `incomplete` | `assessment.processing.status` |
| `X-Einvoice-Report-Scope` | `readable`, `complete` | tatsächlich gelieferter Darstellungsumfang |

Alle sechs Header sind bei einem erfolgreichen Bericht Pflicht. Die Header sind die maschinenlesbare
Zusammenfassung. HTML- und PDF-Inhalt bleiben menschenlesbare Berichte.
Rechnungsnummern, Originaldateinamen, Namen, Steuerkennungen oder andere fachliche Inhalte DÜRFEN NICHT in
zusätzliche Antwort-Header oder den Download-Dateinamen aufgenommen werden, weil Header häufiger in
Infrastrukturprotokollen landen. Die gelieferten Namen lauten deshalb fest
`E-Rechnungs-Pruefbericht.html` beziehungsweise `E-Rechnungs-Pruefbericht.pdf`.

Der PDF-Bericht begrenzt zum Schutz vor unkontrolliert großen Mailanhängen die Darstellung auf höchstens 250
Rechnungspositionen, 250 Findings und 50 Rechnungshinweise. Einzelwerte, Gesamttext und Zeilenumbrüche sowie
technische XML-Zeilen, Roh-XML- und KoSIT-Ausschnitte besitzen zusätzliche feste Budgets. Der Bericht weist jede
Kürzung und bei Positionen sowie Findings die dargestellte und ursprüngliche Anzahl sichtbar aus. Würde das
Ergebnis dennoch mehr als 200 Seiten benötigen, liefert der Endpunkt einen kompakten, gültigen Ersatzbericht.
Die vollständigen analysierten Daten bleiben mit `scope=complete` im HTML-Bericht und über `POST /api/analyze`
zugänglich. Die
ursprüngliche Rechnungsdatei bleibt im Mailflow unverändert erhalten; das vollständige ausgewählte XML kann
außerdem byteidentisch über `POST /api/xml` exportiert werden.

## HTTP- und Betriebsfehler

HTTP-Statuscodes beschreiben den Transport beziehungsweise die technische Verarbeitbarkeit, nicht die fachliche
Gültigkeit einer erkannten Rechnung. Insbesondere MÜSSEN `official=rejected` und `internal=errors` bei
abgeschlossener Verarbeitung weiterhin einen erfolgreichen Berichts-Response erhalten.

| Ergebnis | Fehlerklasse | Behandlung im Flow |
|---|---|---|
| `2xx` mit Syntax `CII` oder `UBL` | erfolgreich erkannte E-Rechnung | Schema prüfen und die drei Bewertungsachsen getrennt routen |
| `2xx` mit Syntax `UNKNOWN` | terminaler Kandidatenfehler | Kandidat ist keine unterstützte E-Rechnung; weitere Kandidaten prüfen |
| `413` oder `422` | terminaler Kandidatenfehler | Kandidat unzulässig, strukturell oder in einem öffentlichen Feld zu groß, unlesbar, unsicher oder ohne Rechnungs-XML; weitere Kandidaten prüfen |
| `400`, `404` oder `405` | Integrations- oder Konfigurationsfehler | nicht automatisch wiederholen; in technischen Fehlerpfad geben |
| `401` oder `403` | Authentifizierungs- oder Berechtigungsfehler | nicht automatisch wiederholen; Zugangskonfiguration korrigieren |
| `408`, `429` oder `5xx` | vorübergehender Betriebsfehler | begrenzt wiederholen |
| Verbindungsfehler oder Client-Timeout | vorübergehender Betriebsfehler | begrenzt wiederholen |
| unerwartete Antwort, Schema ungleich `2`, fehlende Pflicht-Header oder nicht lesbarer Bericht | Protokollfehler | nicht als Rechnungsergebnis werten; technischen Fehlerpfad verwenden |

Bei einem zukünftigen API-Fehlerformat SOLL `detail` eine deutsche, für Menschen geeignete Beschreibung und
`type` einen stabilen maschinenlesbaren Fehlercode enthalten. Der Flow DARF fachliche Entscheidungen nicht durch
Textsuche in `detail` treffen.

Der Browser-interne Header `X-Einvoice-UI-Revision` gehört nicht zum Automatisierungsvertrag. Ein korrekt
Bearer-authentifizierter Node-RED-Aufruf sendet ihn nicht und bleibt damit von Browsercache- und
Alt-Tab-Prüfungen unabhängig.

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

## Abnahmeszenarien

API und Flow MÜSSEN mindestens folgende anonymisierte Fälle automatisiert absichern:

1. gültige und fachlich fehlerhafte CII;
2. UBL Invoice und UBL CreditNote;
3. Hybrid-PDF mit eingebetteter Rechnungs-XML;
4. Sicht-PDF ohne XML neben einer gültigen separaten XML;
5. unbekannte, aber wohlgeformte XML-Syntax;
6. beschädigte XML, verbotene DTD/ENTITY und Größenüberschreitung;
7. mehrere unterschiedliche Rechnungsanhänge und byteidentische Duplikate;
8. alle offiziellen Statuswerte einschließlich `unsupported`; bei verpflichtender offizieller Prüfung MUSS
   `unsupported` einen qualifizierten Bericht erzeugen und die Verarbeitung weiterer Kandidaten fortsetzen;
9. `internal` mit `clear`, `attention`, `errors` und `not-run`;
10. `processing` mit `complete`, `limited` und `incomplete` sowie die Invariante
    `internal=not-run`/`processing=complete`;
11. fehlende, frühere und unbekannte Schemaversionen sowie fehlende oder unbekannte Pflicht-Header;
12. API-Verbindungsfehler, Timeout, `401`/`403`, `422`, `429` und `5xx`;
13. SMTP-Fehler nach erfolgreicher Prüfung;
14. Wiederholung ohne unkontrollierten doppelten Bericht;
15. Quittierung ausschließlich nach einem definierten Abschlusszustand;
16. `BG-16` als fachliche Referenz getrennt von `occurrence` und `xml_location`.
