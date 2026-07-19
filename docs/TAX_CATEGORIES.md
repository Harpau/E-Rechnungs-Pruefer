# Umsatzsteuerkategorien

## Darstellungsprinzip

Die Rechnungsansicht bewahrt den maschinenlesbaren Code und ergänzt eine verständliche Bezeichnung. Sie zeigt Basisbetrag, Steuerbetrag, Steuersatz, Begründung und Begründungscode gemeinsam. Ein Freitext darf nicht nur deshalb verschwinden, weil eine Bemessungsgrundlage vorhanden ist.

| Code | Darstellung | Erwartung der internen Prüfung |
|---|---|---|
| `S` | Standardsteuersatz | positiver Steuersatz |
| `Z` | Nullsteuersatz | Steuersatz `0`, Steuerbetrag `0` |
| `E` | Steuerbefreit | Steuersatz `0`, Steuerbetrag `0`, Begründung erwartet |
| `AE` | Steuerschuldnerschaft des Leistungsempfängers | Steuersatz `0`, Steuerbetrag `0`, Begründung erwartet |
| `O` | Nicht der Umsatzsteuer unterliegend | kein Steuersatz, Steuerbetrag `0`, Begründung erwartet |
| `G` | Steuerfreie Ausfuhr außerhalb der EU | Steuersatz `0`, Steuerbetrag `0`, passende Ausfuhrbegründung erwartet |
| `K` | Innergemeinschaftliche Lieferung | Steuersatz `0`, Steuerbetrag `0`, Begründung erwartet |
| `L` | IGIC | Steuersatz und Berechnung gemäß XML |
| `M` | IPSI | Steuersatz und Berechnung gemäß XML |

## Besonderheit Kategorie O

`O` bedeutet, dass der Umsatz nicht unter die Umsatzsteuer fällt. Ein vorhandener Steuerbetrag ist zwar null, aber das ist nicht dasselbe wie ein Umsatz mit einem Steuersatz von null Prozent. Deshalb:

- zeigt die Anwendung bei fehlendem Rate-Feld keine `0 %` an;
- heißt der Basisbetrag „Nettobetrag dieser Steuerkategorie“;
- wird ein dennoch vorhandener Rate-Wert als Fehler gemeldet;
- darf eine O-Steueraufschlüsselung nicht mit anderen Steueraufschlüsselungen kombiniert werden;
- müssen die Positionen ebenfalls der Kategorie O zugeordnet sein.

## Semantische Widersprüche

Technische Validatoren können häufig nur prüfen, ob ein Begründungsfeld vorhanden ist. Sie verstehen nicht sicher, ob der natürliche Sprachtext zum Code passt. Die interne Prüfung weist deshalb auf einige typische Widersprüche hin:

- `G` zusammen mit „nicht im Inland steuerbar“, „outside the scope“ oder Reverse-Charge-Text;
- `O` zusammen mit einem eindeutigen Ausfuhrhinweis.

Diese Befunde sind Warnungen. Sie ändern den XML-Code nicht und treffen keine endgültige steuerrechtliche Entscheidung.

## Beispiel: sonstige Leistung an einen Drittlandsunternehmer

Für eine gewöhnliche B2B-Dienstleistung, deren Leistungsort außerhalb Deutschlands liegt, kann die maschinenlesbare Einordnung typischerweise `O` sein. Die konkrete steuerliche Behandlung hängt jedoch vom Geschäftsvorfall, den Ortsregeln und dem Recht des Empfängerstaates ab.

CII-Beispiel:

```xml
<ram:ApplicableTradeTax>
  <ram:CalculatedAmount>0.00</ram:CalculatedAmount>
  <ram:TypeCode>VAT</ram:TypeCode>
  <ram:ExemptionReason>
    Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG
  </ram:ExemptionReason>
  <ram:BasisAmount>495.00</ram:BasisAmount>
  <ram:CategoryCode>O</ram:CategoryCode>
</ram:ApplicableTradeTax>
```

Bei Kategorie O wird `RateApplicablePercent` nicht angegeben.

## Pflege

Codebezeichnungen in `app/code_lists.py` dienen der Darstellung. Vollständige Codelist-Gültigkeit bleibt Aufgabe der jeweils eingesetzten offiziellen Regelartefakte. Jede Änderung benötigt Parser-, Validierungs- und Berichtstests.
