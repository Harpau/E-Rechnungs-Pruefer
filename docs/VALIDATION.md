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

Die Registry `CEN-EN16931-validation-1.3.16` enthält exakt diese 62 UNTDID-1001-Codes:

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
und `503` seit CEN 1.3.16 dem UBL-Root `CreditNote` zugeordnet (bis CEN 1.3.15: `Invoice`); Code `81` ist für
beide UBL-Roots zugelassen. Damit umfasst die UBL-`Invoice`-Liste 50 und die UBL-`CreditNote`-Liste 13 Codes;
die CII-Liste enthält unverändert alle 62 Codes.

## Profil- und Rollensemantik

Die Profilauflösung erkennt XRechnung, EN 16931, Peppol Billing, Peppol Self-Billing, Factur-X und ZUGFeRD.
Unbekannte und fehlende Profile erhalten nur partielle interne Fähigkeiten. Die gebündelte offizielle Prüfung
wird ausschließlich durch die exakte Auswahl eines Regelsatzes aus `app/official_rulesets.py` freigeschaltet
(Abschnitte „Gebundene KoSIT-Regelsätze“ und „Factur-X EXTENDED“); die Profilfamilie allein genügt nicht. Ein
erkanntes Profil ohne gebundenes Szenario, etwa `xrechnung_2.3`, eine eigene CIUS oder eine UBL-Gutschrift mit
XRechnung-Extension-Kennung, führt bei angeforderter offizieller Prüfung zu `official.status = "unsupported"` und
startet KoSIT nicht.

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

Jeder KoSIT-Lauf ist an genau einen Regelsatz gebunden. Die Anwendung übergibt die unveränderten Original-XML-Bytes
unter dem festen Namen `rechnung.xml`, verwendet ein temporäres Ausgabeverzeichnis und liest ausschließlich die von
KoSIT geschriebene Berichtsdatei `rechnung-report.xml`. Der Konsolenparameter `--print` wird nicht verwendet;
Berichte aus `stdout`, `stderr` oder dem `[Format error!]`-Wrapper werden nicht mehr ausgewertet, weil sie sich
nicht an Szenario und Dokument binden lassen. Ohne gewählten Regelsatz wird kein Prozess gestartet.

Der Bericht wird gebunden an:

- Wurzel `rep:report` im Namespace `http://www.xoev.de/de/validator/varl/1` mit `rep:engine/rep:name`;
- `rep:scenarioMatched/s:scenario/s:name` gleich dem im Regelsatz hinterlegten Szenarionamen;
- `rep:documentIdentification/rep:documentHash` mit Algorithmus `SHA-256` und gültigem Base64, dessen Wert dem
  SHA-256 der übergebenen Bytes entspricht;
- genau ein `rep:assessment` mit genau einem Kind `rep:accept` oder `rep:reject`.

Entscheidungsreihenfolge:

1. Vorprüfung schlägt fehl (Java, JAR, Szenariodatei, Ressourcenverzeichnis, gebundene Artefakte) → `unavailable`
   mit dem Verarbeitungsbefund `KOSIT-CONFIG`, KoSIT wird nicht gestartet;
2. Start-, Laufzeit- oder Zeitüberschreitungsfehler → `indeterminate` mit `KOSIT-START`, `KOSIT-EXEC` oder
   `KOSIT-TIMEOUT`;
3. `<rep:noScenarioMatched>` im Bericht oder ein anderer Szenarioname als erwartet → keine Rechnungsentscheidung,
   `indeterminate` mit `KOSIT-SCENARIO` (KoSIT lehnt ein Dokument ohne passendes Szenario ab, das sagt nichts
   über die Rechnung aus; der Befund nennt gefundenen und erwarteten Namen);
4. fehlender Bericht, falscher Namespace, fehlende Engine, fehlender, abweichender oder ungültig kodierter
   Dokument-Hash, unbekannter Hash-Algorithmus, keine, mehrere oder widersprüchliche Bewertungen →
   `indeterminate` mit `KOSIT-REPORT`;
5. `<rep:assessment><rep:accept/></rep:assessment>` → `accepted`;
6. `<rep:assessment><rep:reject/></rep:assessment>` → `rejected`.

Weder das Attribut `valid` noch textuelle Statusangaben noch der Prozess-Rückgabecode ersetzen die Entscheidung.
Widersprechen sich Bericht und Rückgabecode, bleibt die gebundene XML-Entscheidung maßgeblich; die Abweichung wird
als Warnung `KOSIT-RESULT-MISMATCH` ausgewiesen. Java-, JAR-, Konfigurations- und Timeoutfehler sind niemals eine
KoSIT-Ablehnung. Regelkennungen stammen aus dem Attribut `code` der `rep:message`-Elemente des angewendeten
Szenarios; offizielle Befunde tragen Profilkennung und Regelstand in `rule.profile` und `rule.version`, die
eingebettete XHTML-Darstellung des Berichts wird nicht ausgewertet.

Die Zusammenfassung nennt Regelsatz, Bewertung, Prüfszenario, Regelstand und Herausgeber, zum Beispiel
`KoSIT-Prüfung erfolgreich: Rechnung wurde nach XRechnung 3.0.2 (UBL Invoice) akzeptiert. Bewertung im Bericht:
accept. Prüfszenario: EN16931 XRechnung (UBL Invoice). Regelstand: XRechnung 3.0.2 / Validator-Konfiguration
2026-08-31 / EN 16931 1.3.16 / XRechnung-Schematron 2.6.0 (KoSIT), KoSIT Validator als Prüfmaschine.`
`assessment.official.scenario` enthält den gebundenen Szenarionamen; Browser, HTML- und PDF-Bericht zeigen denselben
Zusammenfassungstext, der PDF-Bericht zusätzlich die Zeile „Prüfszenario“.

`packaging/kosit/components.lock.json` pinnt KoSIT Validator 1.6.3, XRechnung 3.0.2,
Validator-Konfiguration 2026-08-31, CEN EN 16931 1.3.16 und XRechnung-Schematron 2.6.0 samt Artefakt-Hashes
sowie die Factur-X-1.09.2-EXTENDED-Artefakte aus `fnfempe/France_RFE` v1.4.0.03 (Commit `a63e6b5`) je Datei mit
Größe und SHA-256. `/api/health` nennt diese Versionen unter `kosit.components`, meldet in
`kosit.xrechnung_configured`, ob die gebundene XRechnung-Konfiguration vollständig und unverändert eingerichtet ist,
und in `kosit.facturx_extended_configured` dasselbe für das Extended-Regelpaket; `kosit.configured` sagt nur, dass
JAR, Java, Szenariodatei und Ressourcenverzeichnis vorhanden sind.

## Gebundene KoSIT-Regelsätze für XRechnung und EN 16931

`app/official_rulesets.py` bindet die elf Szenarien der KoSIT-Validator-Konfiguration XRechnung 2026-08-31 an
Wurzelelement, Namespace und rohe Profilkennung. Die Kennung wird vor jeder Normalisierung direkt aus dem
Originalbaum gelesen: bei CII genau ein Element `GuidelineSpecifiedDocumentContextParameter/ID`, bei UBL genau ein
Element `cbc:CustomizationID` unterhalb der Wurzel, jeweils ohne Kindknoten und mit exakt übereinstimmendem Text.
Das ist bewusst strenger als die KoSIT-XPath-Ausdrücke `text() = '…'` und `. = '…'`, die eine Kennung mit
Kommentar teilweise noch akzeptieren.

| Wurzelelement | Kennung | Szenario | Regelsatz |
|---|---|---|---|
| `Invoice` (UBL) | `urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0` | `EN16931 XRechnung (UBL Invoice)` | `xrechnung-3.0.2-ubl-invoice` |
| `Invoice` (UBL) | `…xrechnung_3.0#conformant#urn:xeinkauf.de:kosit:extension:xrechnung_3.0` | `EN16931 XRechnung Extension (UBL Invoice)` | `xrechnung-3.0.2-extension-ubl-invoice` |
| `Invoice` (UBL) | `…xrechnung_3.0#compliant#urn:xeinkauf.de:kosit:xrechnung:cvd_0.9` | `EN16931 XRechnung CVD (UBL Invoice)` | `xrechnung-3.0.2-cvd-ubl-invoice` |
| `CreditNote` (UBL) | `urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0` | `EN16931 XRechnung (UBL CreditNote)` | `xrechnung-3.0.2-ubl-creditnote` |
| `CreditNote` (UBL) | `…xrechnung_3.0#compliant#urn:xeinkauf.de:kosit:xrechnung:cvd_0.9` | `EN16931 XRechnung CVD (UBL CreditNote)` | `xrechnung-3.0.2-cvd-ubl-creditnote` |
| `CrossIndustryInvoice` (CII) | `urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0` | `EN16931 XRechnung (CII)` | `xrechnung-3.0.2-cii` |
| `CrossIndustryInvoice` (CII) | `…xrechnung_3.0#conformant#urn:xeinkauf.de:kosit:extension:xrechnung_3.0` | `EN16931 XRechnung Extension (CII)` | `xrechnung-3.0.2-extension-cii` |
| `CrossIndustryInvoice` (CII) | `…xrechnung_3.0#compliant#urn:xeinkauf.de:kosit:xrechnung:cvd_0.9` | `EN16931 XRechnung CVD (CII)` | `xrechnung-3.0.2-cvd-cii` |
| `Invoice` (UBL) | `urn:cen.eu:en16931:2017` | `EN16931 (UBL Invoice)` | `en16931-ubl-invoice` |
| `CreditNote` (UBL) | `urn:cen.eu:en16931:2017` | `EN16931 (UBL CreditNote)` | `en16931-ubl-creditnote` |
| `CrossIndustryInvoice` (CII) | `urn:cen.eu:en16931:2017` | `EN16931 (CII)` | `en16931-cii` |

Die Konfiguration kennt kein Szenario „XRechnung Extension (UBL CreditNote)“; eine UBL-Gutschrift mit der
Extension-Kennung bleibt `unsupported`. Der Regelstand lautet für XRechnung-Szenarien
`XRechnung 3.0.2 / Validator-Konfiguration 2026-08-31 / EN 16931 1.3.16 / XRechnung-Schematron 2.6.0`, für reine
EN-16931-Szenarien `EN 16931 1.3.16 / Validator-Konfiguration 2026-08-31`; beide werden aus
`app/component_versions.py` gebildet, Herausgeber ist KoSIT.

| Dokument | Auswahl | Status | interner Befund |
|---|---|---|---|
| exakte Kennung am passenden Wurzelelement | Regelsatz | gebundene Prüfung | – |
| Kennung mit Leerraum oder Zeilenumbruch | keine | `unsupported` | `PROFILE-002` |
| Kennung durch Kommentar geteilt oder mit nachgestelltem Kommentar | keine | `unsupported` | `PROFILE-002` |
| mehrere `CustomizationID` oder `GuidelineSpecifiedDocumentContextParameter` | keine | `unsupported` | `PROFILE-003` |
| abweichende Schreibweise, `xrechnung_2.3`, `#conformant#` statt `#compliant#`, eigene CIUS | keine | `unsupported` | – |
| UBL-Gutschrift mit Extension-Kennung, UBL-Datei mit Factur-X-Kennung | keine | `unsupported` | – |

Die Peppol-Geschäftsprozesskennung (BT-23) beeinflusst die Auswahl nicht. Die tolerante Profilerkennung in
`app/profiles.py` bleibt für Anzeige und Dokumenttypsemantik bestehen, schaltet aber keine offizielle Prüfung frei.

### Vorprüfung der Konfiguration

Alle elf Regelsätze verwenden die gemeinsame Konfiguration aus `KOSIT_SCENARIOS` und `KOSIT_REPOSITORIES`
(Standard `vendor/kosit/xrechnung/`). Vor jedem Lauf müssen Java, das Standalone-JAR, genau eine Szenariodatei und
genau ein Ressourcenverzeichnis vorhanden sein. Die Szenariodatei und die 25 Dateien, die die Szenarien direkt oder
über `schemaLocation` und `xsl:import` laden (vier CII-XSD, 15 UBL-XSD für Invoice und CreditNote, die
Schematron-Stylesheets EN 16931 und XRechnung für CII und UBL sowie `xrechnung-report.xsl` und
`default-report.xsl`), müssen Größe und SHA-256 der gepinnten Konfiguration 2026-08-31 entsprechen. Die Hashes
stehen in `app/official_rulesets.py`; die Sperrdatei pinnt weiterhin das ZIP als Ganzes, und die Integrationssuite
prüft beide Angaben gegen eine frische Installation. Jede Abweichung ergibt `unavailable` mit `KOSIT-CONFIG`, ohne
dass Java gestartet wird.

Eine andere Szenariodatei über `KOSIT_SCENARIOS` oder ein anderes Ressourcenverzeichnis über `KOSIT_REPOSITORIES`
schlägt deshalb bereits in der Vorprüfung fehl; eine fremde Konfiguration, die die Vorprüfung besteht, aber andere
Szenarionamen liefert, ergibt `indeterminate` mit `KOSIT-SCENARIO`. Das Extended-Regelpaket bleibt davon
unberührt und umgekehrt.

| Situation | `assessment.official.status` | Verarbeitung |
|---|---|---|
| Kennung ohne gebundenes Szenario | `unsupported` | `complete`, sofern keine andere Begrenzung vorliegt |
| Vorprüfung schlägt fehl (Java, JAR, Szenariodatei, Regeldateien, Hashes, Anzahl der Pfade) | `unavailable` | `incomplete` |
| Start-, Laufzeit-, Timeout- oder Bindungsfehler nach bestandener Vorprüfung | `indeterminate` | `incomplete` |
| gebundener Bericht nimmt an | `accepted` | `complete`, sofern keine andere Begrenzung vorliegt |
| gebundener Bericht lehnt ab | `rejected` | `complete`, sofern keine andere Begrenzung vorliegt |

## Factur-X EXTENDED

Die Anwendung ist eine Anwendung der Norm EN 16931; Startseite, HTML- und PDF-Bericht zeigen diesen Hinweis in der
Fußzeile. Die offizielle Prüfung von Factur-X EXTENDED verwendet die von FNFE-MPE und FeRD veröffentlichten technischen
Artefakte und trifft keine Aussage im Namen dieser Herausgeber.

### Auswahl des Regelsatzes

`app/official_rulesets.py` bindet genau die Kombination aus Wurzelelement `CrossIndustryInvoice`, CII-Namespace
und der rohen Kennung `urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended` an den Regelsatz
`facturx-extended-1.09.2`, nach denselben Regeln wie die XRechnung- und EN-16931-Regelsätze (Abschnitt
„Gebundene KoSIT-Regelsätze“): genau ein Element `GuidelineSpecifiedDocumentContextParameter/ID` ohne Kindknoten,
dessen Text exakt übereinstimmt.

| Dokument | Auswahl | Status | interner Befund |
|---|---|---|---|
| exakte Kennung, CII | Regelsatz | gebundene Prüfung | – |
| Kennung mit Leerraum oder Zeilenumbruch | keine | `unsupported` | `PROFILE-002` |
| Kennung durch Kommentar geteilt oder mit nachgestelltem Kommentar | keine | `unsupported` | `PROFILE-002` |
| mehrere `GuidelineSpecifiedDocumentContextParameter` | keine | `unsupported` | `PROFILE-003` |
| abweichende Schreibweise, `#compliant#`, Suffix, ZUGFeRD-2.0-Kennung | keine | `unsupported` | – |
| UBL-Rechnung mit derselben Kennung | keine | `unsupported` | – |

Die Peppol-Geschäftsprozesskennung (BT-23) beeinflusst die Auswahl nicht.

### Regelstand

Die Kennung `1p0:extended` ist seit Factur-X 1.0 unverändert und nennt keine Spezifikationsversion. Der Prüfer legt
den Regelstand fest: Factur-X 1.09.2 EXTENDED, gemeinsames Release von FeRD und FNFE-MPE vom 2026-08-04, Artefakte
aus `fnfempe/France_RFE` Tag v1.4.0.03 (bytegleich mit dem technischen Teil des FeRD-Pakets ZUGFeRD 2.5.2). Der
Hotfix `fix-FR04` aus v1.4.0.04 wird nicht verwendet. Die davon betroffenen Regeln `BR-FXEXT-S/Z/E/AE/IC/G/O/AF/AG-01`
(Unterpositions-XPath) und `BR-FXEXT-CO-10/12/13` (Rundung) gelten als bekannte Abweichungen des Release 1.09.2:
Ein offizieller Befund mit einer dieser Kennungen bleibt unverändert und erhält zusätzlich den Infobefund
`KOSIT-RULE-NOTICE` auf der Verarbeitungsachse. Die Übernahme der Korrektur ist für Factur-X 1.10 / ZUGFeRD 2.6
angekündigt.

### Vorprüfung und gebundener Lauf

Vor jedem Extended-Lauf müssen Java, das Standalone-JAR, die Szenariodatei `scenarios.xml`, das Report-Stylesheet
`resources/report/default-report.xsl` und alle acht gebundenen Artefakte mit Größe und SHA-256 vorhanden sein.
Jede Abweichung ergibt `unavailable` mit dem Befund `KOSIT-CONFIG`. Der XRechnung-Pfad bleibt davon unberührt; ein
fehlendes Extended-Paket beeinträchtigt EN-16931- und XRechnung-Prüfungen nicht.

Der Lauf verwendet die eigenen Pfade `KOSIT_FACTURX_EXTENDED_SCENARIOS` und `KOSIT_FACTURX_EXTENDED_REPOSITORIES`
(Standard `vendor/kosit/facturx-extended/`) und bindet den Bericht wie in „KoSIT-Auswertung und Versionen“
beschrieben, mit dem erwarteten Szenarionamen `Factur-X 1.09.2 EXTENDED (CII)`. Warnregeln der Artefakte wie
`PEPPOL-EN16931-R008` erscheinen als Warnungen.

| Situation | `assessment.official.status` | Verarbeitung |
|---|---|---|
| Vorprüfung schlägt fehl (Java, JAR, Szenariodatei, Regeldateien, Hashes) | `unavailable` | `incomplete` |
| Start-, Laufzeit-, Timeout- oder Bindungsfehler nach bestandener Vorprüfung | `indeterminate` | `incomplete` |
| gebundener Bericht nimmt an | `accepted` | `complete`, sofern keine andere Begrenzung vorliegt |
| gebundener Bericht lehnt ab | `rejected` | `complete`, sofern keine andere Begrenzung vorliegt |

Eine erfolgreiche XML-Prüfung ist keine Aussage zur PDF/A-Konformität einer hybriden Rechnung; PDF, XMP-Metadaten
und sichtbare Rechnungsinhalte werden nicht geprüft.

## Bekannte Grenzen

- keine Prüfung, ob eine Leistung tatsächlich erbracht oder eine Zahlung tatsächlich ausgeführt wurde;
- keine Echtheits- oder Signaturprüfung;
- keine Prüfung von Handelsregister-, Steuer-ID- oder Kontoinhaberdaten gegen externe Register;
- keine vollständige juristische Würdigung von Leistungsort, Steuerbefreiung oder Reverse Charge;
- keine Garantie, dass ein externer Validator dieselben Versionen der Regelartefakte verwendet;
- keine Prüfung der PDF/A-Konformität, der XMP-Metadaten oder des Sichtteils hybrider Rechnungen;
- keine OCR-Interpretation visueller PDFs.
