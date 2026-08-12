# Prüfmodell

## Grundsatz

Analyseschema 2 trennt drei Fragen, die nicht zu einem gemeinsamen Status verdichtet werden dürfen:

1. Hat ein offizielles Regelwerk die Rechnung angenommen oder abgelehnt?
2. Welche internen Vorprüfungen und Plausibilitätsbefunde gibt es?
3. Wurden die angeforderten technischen Verarbeitungsschritte vollständig abgeschlossen?

Eine erkannte CII- oder UBL-Rechnung bleibt auch bei offizieller Ablehnung oder internen Fehlern eine erkannte
E-Rechnung. Umgekehrt sind eine erfolgreiche Verarbeitung und eine unauffällige interne Prüfung kein Beleg für
rechtliche, steuerliche oder wirtschaftliche Richtigkeit.

## Sichere Lesbarkeit und strikte Werte

`app/analyzer.py` erkennt nur diese exakten Root-/Namespace-Kombinationen:

- CII `CrossIndustryInvoice` im Namespace
  `urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100`;
- UBL `Invoice` im Namespace `urn:oasis:names:specification:ubl:schema:xsd:Invoice-2`;
- UBL `CreditNote` im Namespace `urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2`.

Ein anderer wohlgeformter Root wird als `capabilities.syntax = "UNKNOWN"` behandelt. Die interne Prüfung steht
dann auf `not-run`; `assessment.processing` steht mit `SYNTAX-001` auf `incomplete`. Nicht wohlgeformtes XML,
DTD-/ENTITY-Deklarationen und nicht sicher extrahierbare PDF-Inhalte werden bereits als Eingabefehler
zurückgewiesen.

Nach der Root-Erkennung werden auch alle fachlichen Kindelemente ausschließlich über die für UBL beziehungsweise
CII festgelegten Namespace-URIs gelesen. XML-Präfixe sind frei wählbar; ein gleichnamiges Element aus einem
fremden oder vertauschten Namespace bleibt technisch sichtbar, darf aber kein normalisiertes Fachfeld liefern.

Rechenwerte werden mit `Decimal` verarbeitet. Für XML-Decimal-Felder akzeptiert die Anwendung nur endliche
Dezimaldarstellungen ohne Exponent. `NaN`, `Infinity`, `-Infinity`, Dezimalkomma, Exponentialschreibweise und
freie Texte werden nicht stillschweigend konvertiert. Ein ungültiger Ausgangswert erzeugt den zuständigen
internen Befund; abhängige Rechenregeln werden nicht mit erfundenen Ersatzwerten fortgesetzt. Die
Berechnungstoleranz beträgt zwei Cent. Ein einzelner Decimal-Operand darf höchstens 4.096 Ziffern enthalten.
Diese weit oberhalb üblicher Rechnungsbeträge liegende Ressourcengrenze hält den Rechenkontext auch bei
nichtterminierenden Divisionen beschränkt; eine Überschreitung endet wertfrei mit `422 invoice_input_error`.

Fehlend und vorhanden-aber-ungültig bleiben getrennt: Für ein ungültiges Rechnungsdatum oder einen ungültigen
Zahlbetrag wird kein Pflichtfeld-„fehlt“-Befund erzeugt. Der normalisierte Wert bleibt `null`, der Rohwert bleibt
im technischen Anhang, `assessment.internal.status` steht auf `errors` und `CHECK-000` wird nicht ausgegeben.

Fachlich verschiedene Angaben bleiben auch dann getrennt, wenn eine Syntax sie räumlich zusammenfasst:
`delivery.actual_date` enthält den tatsächlichen Liefertermin (BT-72), `delivery.location` den Lieferort
(BT-71/BG-15), `parties.delivery_recipient` nur eine tatsächlich angegebene Lieferpartei und
`periods.delivery` nur einen wirklichen Liefer-/Leistungszeitraum. UBL `InvoicePeriod/DescriptionCode` wird als
Steuerzeitpunktcode BT-8 behandelt und nicht als Zeitraumtext; ein UBL-Hinweisartcode BT-21 bleibt strukturiert
am jeweiligen Dokumenthinweis erhalten.

## Dokumenttyp-Registry

Die Registry `CEN-EN16931-validation-1.3.15` enthält exakt diese 62 UNTDID-1001-Codes:

```text
71 80 81 82 83 84 102 130 202 203 204 211 218 219 261 262 295 296 308
325 326 331 380 381 382 383 384 385 386 387 388 389 390 393 394 395 396
420 456 457 458 471 472 473 500 501 502 503 527 532 553 575 623 633 751
780 817 870 875 876 877 935
```

`document.type.status` unterscheidet:

- `known`: Code ist in der Registry enthalten; Bezeichnung, Familie, Grundpolarität, Settlement-Relevanz und
  Self-Billing-Eigenschaft sind verfügbar;
- `unknown`: ein Rohcode ist vorhanden, aber nicht registriert; er bleibt in `document.type.code.value`
  erhalten, ohne eine Dokumentfamilie zu erfinden;
- `missing`: kein verwendbarer Code vorhanden.

`capabilities.document_type_recognition` spiegelt dies als `recognized`, `unknown` oder `missing`. Für UBL wird
zusätzlich `document.type.ubl_root` (`invoice` oder `credit-note`) und `root_compatibility` ausgewiesen. Ein
bekannter Code am falschen UBL-Root ist `incompatible` und erzeugt `BR-CL-01`; unbekannte oder fehlende Codes
bleiben `undetermined`. Für CII ist der Root-Abgleich `not-applicable`. Versionsspezifisch sind die Codes `502`
und `503` in CEN 1.3.15 dem UBL-Root `Invoice` zugeordnet; Code `81` ist für beide UBL-Roots zugelassen.

## Profil- und Rollensemantik

Die Profilauflösung erkennt XRechnung, EN 16931, Peppol Billing, Peppol Self-Billing, Factur-X und ZUGFeRD.
Unbekannte und fehlende Profile erhalten nur partielle interne Fähigkeiten. Die gebündelte offizielle Prüfung
ist auf die erkannten EN-16931-/XRechnung-Fälle begrenzt; ein erkanntes, aber nicht gebündeltes Profil führt bei
angeforderter offizieller Prüfung zu `official.status = "unsupported"` und startet KoSIT nicht.

`roles` trennt:

- `issuer` und `document_recipient` für den Dokumentaustausch;
- `creditor` und `debtor` für die wirtschaftliche Grundrolle;
- `expected_payer`, `expected_recipient` und `expected_payment_direction` als aus Dokumenttyp und
  Zahlbetragsvorzeichen abgeleitete Erwartung.

Self-Billing ändert nicht automatisch Gläubiger und Schuldner. Bei Typ `389` stellt typischerweise der Käufer das
Dokument aus; bei positivem Zahlbetrag bleibt die erwartete Richtung dennoch Käufer → Verkäufer. Bei einer
positiven Gutschrift des Typs `381` ist die erwartete Richtung Verkäufer → Käufer. Ein negativer Zahlbetrag
kehrt die typbezogene Wirkung um, ein Nullbetrag ist neutral. Widersprechen sich ein sicher erkanntes
Self-Billing-Profil und der Dokumenttyp, werden die betroffenen Rollen nicht geraten.

Diese Rollen beschreiben keine tatsächlich erfolgte, rechtlich geschuldete oder künftig zwingende Zahlung.
Vorauszahlungen, Aufrechnung, vertragliche Abwicklung und ein ausdrücklicher Zahlungsempfänger können vom
abgeleiteten Erwartungsbild abweichen.

## Interne Regeln

Die interne Prüfung untersucht unter anderem:

- zentrale Pflichtfelder, Profilkennung, Codes, Datumsfolgen und Adressen;
- Positionsnummern, Mengen, Einheiten, Preise, Preisbasismengen und Nachlässe/Zuschläge;
- Kopf-, Steuer-, Netto-, Brutto-, Vorauszahlungs-, Rundungs- und Zahlbeträge;
- Steuerkategorien, Steuersätze, Bemessungsgrundlagen und Begründungen;
- IBAN-Prüfziffer, BIC-Format und ausgewählte Kontoinhaberplausibilität;
- Dokumenttyp-/UBL-Root-Kompatibilität und ausgewählte Profilregeln.

Die folgenden Zahlungs- und Typregeln sind bewusst getrennt:

| Ausgegebene ID | Referenz | Severity | Exakte interne Bedingung |
|---|---|---|---|
| `BR-CO-25` | EN 16931 BR-CO-25 | Fehler | positiver Zahlbetrag, aber weder Fälligkeitsdatum noch inhaltliche Zahlungsbedingung |
| `BR-49` | EN 16931 BR-49 | Fehler | eine vorhandene Zahlungsanweisung besitzt keinen Zahlungsartcode BT-81 |
| `XRECHNUNG-BR-DE-1` | XRechnung BR-DE-1 | Fehler | sicher erkanntes XRechnung-Profil ohne Zahlungsanweisung BG-16 |
| `XRECHNUNG-BR-DE-17` | XRechnung BR-DE-17 | Warnung | XRechnung verwendet einen Code außerhalb `326, 380, 381, 384, 389, 875, 876, 877` |

Ein positiver Zahlbetrag löst somit keine allgemeine Pflicht zu einem Zahlungsweg aus. BG-16 wird nur über die
profilbezogene XRechnung-Regel verlangt. Ist eine Zahlungsanweisung vorhanden, verlangt BR-49 darin BT-81.
BR-CO-25 betrifft unabhängig davon Fälligkeit oder Zahlungsbedingungen.

Die internen Regeln sind keine vollständige Umsetzung sämtlicher EN-16931-, XRechnung- oder Peppol-Regeln und
keine Steuerberatung.

## Strukturierte Befunde

Jeder Schema-2-Befund enthält:

- `origin`: `official`, `internal` oder `processing`;
- `rule_class` und `severity`;
- `rule` mit stabiler ID, Titel, Nachricht, Quelle, Referenz, optionalem Profil und Version;
- `semantic_references` für fachliche BG-/BT-Bezüge;
- `occurrence` für Scope, nullbasierten Arrayindex, fachliche Kennung und optionalen JSON-Pointer;
- `xml_location` nur für einen tatsächlich bekannten XML-Pfad beziehungsweise Zeile/Spalte;
- optional `actual` und `expected` als Evidenz.

`BG-16` bedeutet fachlich „Zahlungsanweisungen“. Es ist weder ein Ort im Bericht noch ein JSON- oder XML-Pfad.
Consumer müssen für das Analyseobjekt `occurrence.json_pointer` und für die XML-Quelle `xml_location` verwenden.
Die Zahl in einer menschenlesbaren Bezeichnung wie „Position 42“ ist keine Arrayposition; die erste Position mit
der fachlichen ID `42` hat `occurrence.index = 0`, `identifier = "42"` und `/lines/0`.

Regel-IDs werden nach Veröffentlichung nicht für eine andere Bedeutung wiederverwendet. Wichtige Präfixe:

| Präfix | Bereich |
|---|---|
| `REQ`, `PROFILE` | Pflichtangaben und Profilkennung |
| `BR`, `XRECHNUNG-BR` | transparente Vorprüfung benannter EN-/XRechnung-Regeln |
| `CODE`, `CURR`, `ADDR` | Codes, Währungen und Adressen |
| `FORMAT`, `DATE`, `LINE`, `CALC` | lexikalische Werte, Datumslogik, Positionen und Berechnungen |
| `TAX`, `PAY` | Steuer- und Zahlungsplausibilität |
| `TECH`, `SYNTAX`, `KOSIT` | technische Verarbeitung und offizielle Anbindung |

## Drei unabhängige Statusachsen

### `assessment.official`

| Status | Bedeutung |
|---|---|
| `accepted` | auswertbarer offizieller Bericht enthält eine Annahme |
| `rejected` | auswertbarer offizieller Bericht enthält eine Ablehnung |
| `not-requested` | offizielle Prüfung wurde nicht angefordert |
| `unsupported` | Profil ist erkannt, aber nicht im gebündelten offiziellen Regelwerk enthalten |
| `unavailable` | angeforderte offizielle Prüfung ist nicht konfiguriert/verfügbar |
| `indeterminate` | angefordert, aber keine belastbare Entscheidung möglich |

### `assessment.internal`

| Status | Bedeutung |
|---|---|
| `clear` | ausgeführt, keine Warnung und kein Fehler |
| `attention` | ausgeführt, mindestens eine Warnung, aber kein Fehler |
| `errors` | ausgeführt, mindestens ein Fehler |
| `not-run` | interne Prüfung wurde nicht ausgeführt |

### `assessment.processing`

| Status | Bedeutung |
|---|---|
| `complete` | vorgesehene Verarbeitung vollständig abgeschlossen |
| `limited` | abgeschlossen, aber Darstellung oder technischer Umfang begrenzt beziehungsweise technischer Hinweis vorhanden |
| `incomplete` | angeforderte Verarbeitung nicht vollständig abgeschlossen |

Zähler und Befunde gehören jeweils nur zu ihrer Achse. Es existiert kein übergreifender
`ok`-/`warning`-/`invalid`-Status.

## KoSIT-Auswertung und Versionen

Die Integration verwendet ein temporäres Ausgabeverzeichnis und liest primär `*-report.xml`. Der
Konsolenparameter `--print` wird nicht verwendet. Als Rückfall kann ein vollständiger Bericht aus `stdout` oder
`stderr` extrahiert werden, einschließlich des bekannten `[Format error!]`-Wrappers.

Entscheidungsreihenfolge:

1. `<rep:assessment><rep:accept/></rep:assessment>` → `accepted`;
2. `<rep:assessment><rep:reject/></rep:assessment>` → `rejected`;
3. nur bei älteren oder angepassten Berichten ohne Assessment: Prozessrückgabecode als Rückfall;
4. kein valider Bericht → keine offizielle Rechnungsentscheidung.

Widersprechen sich Bericht und Rückgabecode, bleibt die XML-Entscheidung maßgeblich; die Abweichung wird als
technischer Verarbeitungsbefund ausgewiesen. Java-, JAR-, Konfigurations- und Timeoutfehler sind niemals eine
KoSIT-Ablehnung.

`packaging/kosit/components.lock.json` pinnt KoSIT Validator 1.6.2, XRechnung 3.0.2,
Validator-Konfiguration 2026-01-31, CEN EN 16931 1.3.15 und XRechnung-Schematron 2.5.0 samt Artefakt-Hashes.

## Bekannte Grenzen

- keine Prüfung, ob eine Leistung tatsächlich erbracht oder eine Zahlung tatsächlich ausgeführt wurde;
- keine Echtheits- oder Signaturprüfung;
- keine Prüfung von Handelsregister-, Steuer-ID- oder Kontoinhaberdaten gegen externe Register;
- keine vollständige juristische Würdigung von Leistungsort, Steuerbefreiung oder Reverse Charge;
- keine Garantie, dass ein externer Validator dieselben Versionen der Regelartefakte verwendet;
- keine OCR-Interpretation visueller PDFs.
