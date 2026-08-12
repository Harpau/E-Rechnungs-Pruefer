# Migration auf Analyseschema 2

## Geltungsbereich

Analyseschema 2 ist der ausschließlich unterstützte öffentliche Vertrag ab E‑Rechnungs‑Prüfer 2.0.0 und ersetzt
den bisherigen JSON- und Berichtsvertrag sofort am bestehenden API-Endpunkt. Betroffen sind:

- `POST /api/analyze`;
- die intern von `POST /api/report` und `POST /api/report/pdf` verwendete Analyse;
- die maschinenlesbaren Antwortheader der HTML- und PDF-Berichte;
- den wählbaren Darstellungsumfang dieser Berichte (`scope=readable|complete`, Standard `readable`);
- Consumer wie Browseroberfläche, Node-RED-Flows und eigene Automatisierungen.

Es gibt keinen `/api/v1`-Endpunkt, keinen Versionsparameter, keine Content-Negotiation für Schema 1 und keinen
Legacy-Adapter. Server und Consumer müssen gemeinsam umgestellt werden. Ein Consumer muss
`schema_version == 2` beziehungsweise `X-Einvoice-Analysis-Schema: 2` prüfen und jede andere oder fehlende
Version als Protokollfehler behandeln.

Die folgenden Tabellen dokumentieren die zentralen Feldzuordnungen und ausdrücklich bekannten Verschiebungen.
Sie ersetzen keine Bestandsaufnahme der tatsächlich von einem Consumer verwendeten Schema-1-Pfade, da nicht
jedes historische optionale Feld eine direkte Eins-zu-eins-Entsprechung besitzt.

## Geschlossener Top-Level-Vertrag

`POST /api/analyze` liefert genau diese Top-Level-Felder:

```text
schema_version
document
profile
capabilities
parties
roles
periods
delivery
references
lines
allowances_charges
tax
totals
payment
assessment
source
technical
runtime
```

Die öffentlichen Modelle sind geschlossen; zusätzliche Felder werden bei der Modellvalidierung abgewiesen.
Consumer dürfen nicht auf interne Parser-Dictionaries oder nicht dokumentierte Zusatzschlüssel zugreifen.

Wiederkehrende Datentypen sind jetzt strukturiert:

| Typ | Schema-2-Form | Hinweis |
|---|---|---|
| Betrag | `{"value": "119.00", "currency": "EUR"}` | `value` wird als exakte Decimal-Zeichenfolge serialisiert, nicht als binärer JSON-Float |
| Menge | `{"value": "2", "unit": {"value": "H87", ...}}` | Einheit ist ein strukturierter Code |
| Code | `{"value": "380", "label": "Handelsrechnung", "list_id": "UNCL1001"}` | Rohcode, Anzeige und Codeliste bleiben getrennt |
| Kennung | `{"value": "...", "scheme_id": "..."}` | `scheme_id` ersetzt das bisherige uneinheitliche `scheme` |
| Datum | `"2026-07-31"` | ISO-8601-Datum |

## Top-Level- und Dokumentfelder

| Schema 1 | Schema 2 | Migrationshinweis |
|---|---|---|
| `document.syntax` | `capabilities.syntax` | Werte bleiben `CII`, `UBL`, `UNKNOWN` |
| `profile.ubl_version` | `capabilities.syntax_version` | Syntaxversion gehört zu den Fähigkeiten, nicht zum Profil |
| `document.format` | `capabilities.format_name` | reine Formaterkennung |
| `document.profile_id` | `profile.id` | Profilkennung |
| `document.profile_name` | `profile.name` | normalisierte Profilbezeichnung |
| `document.type_code` | `document.type.code.value` | Code bleibt erhalten |
| `document.type_label` | `document.type.code.label` | nur bei bekanntem Code gesetzt |
| `document.kind` | `document.type.family` | geschlossene Dokumentfamilie statt freiem Anzeigetext |
| kein expliziter Typstatus | `document.type.status` | `known`, `unknown` oder `missing` |
| kein Registrybezug | `document.type.registry_version` | derzeit `CEN-EN16931-validation-1.3.15` |
| kein UBL-Abgleich | `document.type.ubl_root` und `.root_compatibility` | Root und Typcode werden getrennt ausgewiesen |
| `document.currency` | `document.document_currency.value` | strukturierter ISO-4217-Code |
| `document.currency_label` | `document.document_currency.label` | Anzeige getrennt vom Rohcode |
| `document.tax_currency` | `document.vat_accounting_currency.value` | Umsatzsteuer-Abrechnungswährung |
| `document.due_date` | `payment.due_date` | Zahlungstermin gehört zum Zahlungsmodell |
| `document.delivery_date` | `delivery.actual_date` | tatsächlicher einzelner Liefertermin (BT-72) |
| kein getrennter Lieferort | `delivery.location.id` und `.postal_address` | Lieferort (BT-71/BG-15) ist keine Partei |
| `document.notes[]` als Text | `document.notes[].text` | optional zusätzlich `subject_code` |
| `seller`, `buyer`, `payee` | `parties.seller`, `.buyer`, `.payee` | Parteien liegen unter einer gemeinsamen Wurzel |
| `invoicee` | `parties.invoice_recipient` | eindeutige Rollenbezeichnung |
| `ship_to` | `parties.delivery_recipient` | Lieferempfänger |
| `seller_tax_representative` | `parties.seller_tax_representative` | Steuervertreter |
| `party.name` | `party.legal_name` | Handelsname steht separat unter `trading_name` |
| `party.description` | `party.additional_legal_information` | rechtliche Zusatzangaben |
| `party.address` | `party.postal_address` | Land ist ein strukturierter Code |
| `party.ids` / `party.tax_ids` | `party.identifiers` / `party.tax_identifiers` | jede Kennung enthält `kind` und `identifier`; direkte Parteien- und rechtliche Registerkennungen bleiben unterscheidbar |
| `delivery.date` | `delivery.actual_date` | Einzeltermin, nicht künstlich als Zeitraum verdoppeln |
| `delivery.location_id` / `.address` | `delivery.location.id` / `.postal_address` | Ort bleibt vom echten Lieferempfänger getrennt |
| Liefer-/Leistungszeitraum | `periods.delivery` | nur ein tatsächlich angegebener Zeitraum; nicht aus BT-72 erzeugen |
| Versand-/Wareneingangsreferenz in `delivery` | `references.despatch_advice` / `.receiving_advice` | Referenzen liegen ausschließlich unter `references` |
| `header_allowances_charges` | `allowances_charges` | gemeinsame strukturierte Nachlass-/Zuschlagsform |

`roles` ist neu. Es trennt Dokumentaussteller und -empfänger von den wirtschaftlichen Rollen
`creditor`/`debtor` und von `expected_payer`/`expected_recipient`. Diese Werte sind aus Dokumenttyp, Profil und
Vorzeichen abgeleitete Erwartungen. Sie sind kein Nachweis, dass eine Zahlung erfolgt ist, erfolgen muss oder
tatsächlich in dieser Richtung abgewickelt wird.

`parties.delivery_recipient` wird nur belegt, wenn die Syntax tatsächlich eine Lieferpartei enthält. Eine bloße
Kennung oder Anschrift des Lieferorts erzeugt keine künstliche Partei. Bei UBL bleibt außerdem
`InvoicePeriod/DescriptionCode` als `document.tax_point_date_code` (BT-8) vom freien Zeitraumtext getrennt;
strukturierte UBL-Hinweisartcodes stehen unter `document.notes[].subject_code` (BT-21).

## Positionen, Steuern und Summen

| Schema 1 | Schema 2 |
|---|---|
| `lines[].name` / `.description` | `lines[].item.name` / `.item.description` |
| `lines[].seller_item_id` | `lines[].item.seller_identifier` |
| `lines[].buyer_item_id` | `lines[].item.buyer_identifier` |
| `lines[].standard_item_id` | `lines[].item.standard_identifier` |
| `lines[].classifications` | `lines[].item.classifications` |
| `lines[].additional_properties` | `lines[].item.properties` |
| `lines[].origin_country` | `lines[].item.origin_country` |
| `lines[].accounting_cost` | `lines[].accounting_reference` |
| `lines[].period.start` / `.end` | `lines[].period.start_date` / `.end_date` |
| `lines[].quantity` + `.unit_code` | `lines[].quantity.value` + `.quantity.unit` |
| `lines[].price` | `lines[].price.net` |
| `lines[].base_quantity` + `.base_unit_code` | `lines[].price.base_quantity` |
| `lines[].gross_price` | `lines[].price.gross` |
| Preisnachlassfelder | `lines[].price.discount.amount` / `.percentage` |
| `lines[].line_total` | `lines[].net_amount` |
| `lines[].tax_category` | `lines[].tax_category.value` |
| `lines[].tax_rate` | `lines[].tax_rate_percent` |
| `taxes[]` | `tax.breakdown[]` |
| `taxes[].category_code` / `.category_display` | `tax.breakdown[].category.value` / `.label` |
| `taxes[].rate` | `tax.breakdown[].rate_percent` |
| `taxes[].basis_amount` | `tax.breakdown[].taxable_amount` |
| `taxes[].tax_amount` | `tax.breakdown[].tax_amount` |
| `taxes[].exemption_reason(s)` | `tax.breakdown[].exemption.reasons[]` |
| `taxes[].exemption_reason_code` | `tax.breakdown[].exemption.reason_code` |
| `totals.line_total` | `totals.line_net_total` |
| `totals.allowance_total` | `totals.allowance_total` |
| `totals.charge_total` | `totals.charge_total` |
| `totals.tax_basis_total` | `totals.tax_exclusive_total` |
| `totals.tax_total` | `tax.totals.document_currency` |
| `totals.tax_total_accounting` | `tax.totals.vat_accounting_currency` |
| `totals.grand_total` | `totals.tax_inclusive_total` |
| `totals.prepaid_amount` | `totals.prepaid_total` |
| `totals.rounding_amount` | `totals.rounding` |
| `totals.due_payable_amount` | `totals.payable` |

Alle genannten Schema-2-Beträge sind `Amount`-Objekte. Consumer müssen daher beispielsweise
`totals.payable.value` statt `totals.due_payable_amount` lesen und dürfen die Währung nicht mehr aus einem
entfernten pauschalen `totals.currency` ableiten.

## Zahlung, Referenzen, Quelle und Technik

| Schema 1 | Schema 2 |
|---|---|
| `payment.means[]` | `payment.instructions[]` |
| `means.type_code` / `.type_label` | `instructions[].means.value` / `.label` |
| `means.information` | `instructions[].instruction_note` |
| `payment.means[].payment_id` | `payment.instructions[].payment_id` |
| Konto, Kontoname und Zahlungsdienstleister direkt im Zahlungsweg | `instructions[].credit_transfers[]` |
| Kartenkonto / Karteninhaber direkt im Zahlungsweg | `instructions[].payment_card.masked_account_identifier` / `.holder_name` |
| Mandats-, Gläubiger- und Belastungskonto direkt im Zahlungsweg | `instructions[].direct_debit` |
| `payment.terms[].direct_debit_mandate_id` | `payment.instructions[].direct_debit.mandate_reference` |
| `payment.terms[].partial_payment_amount` | `payment.terms[].partial_payment` als `Amount` |
| `references.additional_documents` | `references.supporting_documents` |
| einfache Referenzwerte | `Reference` mit `id`, `issue_date` und `description` |
| `source.filename`, `.media_type`, `.size`, `.sha256` | `source.upload` |
| `source.xml_filename`, `.xml_size`, `.xml_sha256` | `source.invoice_xml` |
| `source.container.type` | `source.container.kind` (`xml`, `pdf`, `unknown`) |
| `source.attachments[].size` | `source.attachments[].size_bytes`; zusätzlich `selected` |
| `technical.rows` | `technical.fields` |
| `technical.original_xml` | `technical.source_xml` |
| `technical.raw_xml` | `technical.pretty_xml` |
| `processing.duration_ms` | `runtime.duration_ms` |
| `processing.application_version` | `runtime.application_version` |

Das strukturierte Kartenkonto enthält nur eine Maskierung mit höchstens den letzten vier Zeichen. Erkannte rohe
Kartennummern werden außerdem aus `technical.fields`, `technical.source_xml` und `technical.pretty_xml`
redigiert. `POST /api/xml` bleibt davon bewusst ausgenommen und liefert die ausgewählte Rechnungs-XML
bytegetreu; dieser Endpunkt ist daher kein anonymisierter Export.

## Drei unabhängige Bewertungsachsen

Der frühere Block `validation` entfällt vollständig:

| Schema 1 | Schema 2 | Regel |
|---|---|---|
| `validation.status` | kein Einzelersatz | nicht nachbilden |
| `validation.counts` | `assessment.<axis>.counts` | Zähler je Achse auswerten |
| `validation.findings` | `assessment.<axis>.findings` | Befunde bleiben ihrer Herkunft zugeordnet |
| `validation.builtin` | `assessment.internal` | interne Prüfung |
| `validation.official` | `assessment.official` | offizielle Konformität |
| technischer Zustand in Meldung oder Sammelstatus | `assessment.processing` | technischer Abschluss |

Zulässige Statuswerte:

- `assessment.official.status`: `accepted`, `rejected`, `not-requested`, `unsupported`, `unavailable`,
  `indeterminate`;
- `assessment.internal.status`: `clear`, `attention`, `errors`, `not-run`;
- `assessment.processing.status`: `complete`, `limited`, `incomplete`.

`official=rejected` bedeutet keine fehlgeschlagene Verarbeitung. `internal=errors` ist keine offizielle
Ablehnung. `processing=incomplete` darf nicht als abgeschlossenes Rechnungsergebnis behandelt werden.

## Strukturierte Befunde

Ein Befund besitzt in Schema 2 unter anderem:

```json
{
  "origin": "internal",
  "rule_class": "profile_precheck",
  "severity": "error",
  "rule": {
    "id": "XRECHNUNG-BR-DE-1",
    "title": "Zahlungsanweisungen fehlen",
    "message": "...",
    "source": "XRechnung",
    "reference": "BR-DE-1",
    "profile": "XRechnung",
    "version": null
  },
  "semantic_references": [
    {"id": "BG-16", "label": "Zahlungsanweisungen"}
  ],
  "occurrence": {
    "scope": "payment",
    "index": null,
    "identifier": null,
    "json_pointer": "/payment"
  },
  "xml_location": null,
  "actual": null,
  "expected": null
}
```

`BG-16`, `BT-81` und andere BG-/BT-Kennungen sind fachliche CEN-Referenzen. Sie stehen ausschließlich unter
`semantic_references` und sind weder JSON- noch XML-Orte. Für eine Stelle im Analyseobjekt ist
`occurrence.json_pointer` maßgeblich; eine konkrete XML-Fundstelle darf nur aus `xml_location.path`, `.line` und
`.column` gelesen werden. Ein fehlendes `xml_location` darf nicht durch einen BG-/BT-Code ersetzt werden.
`occurrence.index` ist immer der nullbasierte Index im veröffentlichten Array; eine fachliche Positionskennung
steht separat in `occurrence.identifier`. Zahlen aus einer menschenlesbaren Ortsbezeichnung dürfen nicht als
Index interpretiert werden.

Überschreitet ein untrusted Rechnungswert eine feste Maximallänge des öffentlichen Vertrags, antworten Analyse-,
HTML- und PDF-Endpunkt kontrolliert mit `422 invoice_input_error` statt mit `500`. Werte werden nicht still
gekürzt; der Fehlertext enthält weder den Rohwert noch interne Pydantic-Details.

## Berichtheader

| Bisheriger Header | Schema-2-Header | Werte |
|---|---|---|
| kein Schemaheader | `X-Einvoice-Analysis-Schema` | exakt `2` |
| `X-Einvoice-Syntax` | `X-Einvoice-Syntax` | `CII`, `UBL`, `UNKNOWN` |
| `X-Einvoice-Validation-Status` | kein Einzelersatz | alter Header entfällt |
| `X-Einvoice-Official-Status` | `X-Einvoice-Conformity-Status` | Status von `assessment.official` |
| kein eigener Header | `X-Einvoice-Internal-Status` | Status von `assessment.internal` |
| kein eigener Header | `X-Einvoice-Processing-Status` | Status von `assessment.processing` |
| kein eigener Header | `X-Einvoice-Report-Scope` | `readable` oder `complete` |

Die beiden früheren Statusheader werden nicht zusätzlich gesendet. Ein erfolgreicher HTML- oder PDF-Bericht
muss alle sechs Schema-2-Header enthalten. Der Form-Parameter `scope` ist unabhängig von der Analyseschemaversion:
`readable` enthält Rechnung und menschenlesbare Prüfergebnisse, `complete` zusätzlich die technischen Anhänge.

## Empfohlene Umstellung

1. Consumer so ändern, dass unbekannte oder fehlende Schemaversionen geschlossen abgewiesen werden.
2. Alle vom Consumer verwendeten Schema-1-Zugriffe inventarisieren und anhand der zentralen Zuordnungen sowie
   des geschlossenen Schema-2-Vertrags ersetzen; für nicht aufgeführte optionale Altpfade bewusst festlegen, ob
   sie verschoben, strukturell aufgeteilt oder ersatzlos entfallen sind.
3. Beträge, Codes, Kennungen, Mengen und Notizen als strukturierte Objekte verarbeiten.
4. Offizielle, interne und technische Achse getrennt routen; keinen gemeinsamen Status rekonstruieren.
5. Befund-IDs aus `finding.rule.id` und fachliche Referenzen aus `semantic_references` lesen.
6. Berichtsconsumer atomar auf die sechs neuen Header einschließlich `X-Einvoice-Report-Scope` umstellen.
7. Maskierte Kartendaten nicht mit dem bytegetreuen, unmaskierten `/api/xml`-Export verwechseln.
8. CII, UBL Invoice, UBL CreditNote, unbekannte Syntax sowie alle benötigten Achsen- und Headerwerte vor der
   Produktivumschaltung regressionsprüfen.
