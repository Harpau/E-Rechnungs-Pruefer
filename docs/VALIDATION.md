# Prüfmodell

## Drei getrennte Ebenen

### 1. Sichere Lesbarkeit

Die Datei muss als XML oder als PDF mit eingebetteter XML lesbar sein. Wohlgeformtheit, verbotene DTD-/ENTITY-Deklarationen und unterstützte Wurzelelemente werden hier behandelt.

### 2. Interne Prüfung

Die interne Prüfung ist sofort verfügbar und bewusst transparent. Sie untersucht unter anderem:

- zentrale Pflichtfelder;
- Datumsreihenfolge;
- Positionsnummern und Positionsbezeichnungen;
- `Menge × Preis ÷ Preisbasismenge ± Nachlässe/Zuschläge`;
- Kopf-, Steuer-, Brutto- und Zahlbeträge;
- Währungs- und Einheitenkonsistenz;
- Steuerkategorien und Steuersätze;
- IBAN-Prüfziffer und BIC-Format;
- ausgewählte semantische Widersprüche zwischen Steuerkategorie und Begründung.

Die Geldtoleranz beträgt zwei Cent. Die Regeln sind keine vollständige Umsetzung aller EN-16931-, XRechnung- oder Peppol-Regeln und keine Steuerberatung.

### 3. Offizielle KoSIT-Prüfung

Nach Installation von KoSIT werden die zur Profilkennung passenden XSD- und Schematron-Szenarien ausgeführt. Der technische VARL-Bericht bleibt erhalten und wird in das gemeinsame Ergebnis übernommen.

## Statusbildung

- `invalid`: mindestens ein Fehler aus interner oder ausgeführter offizieller Prüfung
- `warning`: kein Fehler, aber mindestens eine Warnung
- `ok`: weder Fehler noch Warnung

Hinweise verändern den Status nicht.

Wurde KoSIT nicht ausgeführt, lautet die Bewertung ausdrücklich „interne Prüfung“. Eine fehlende Java-Laufzeit, ein falsches JAR, eine ungültige Konfiguration oder ein Timeout darf nie als Ablehnung der Rechnung erscheinen.

## KoSIT-Auswertung

Die Integration verwendet ein temporäres Ausgabeverzeichnis und liest `*-report.xml`. Der Konsolenparameter `--print` wird nicht verwendet. Als Rückfall kann ein vollständiger Bericht aus `stdout` oder `stderr` extrahiert werden, einschließlich des bekannten `[Format error!]`-Wrappers.

Entscheidungsreihenfolge:

1. `<rep:assessment><rep:accept/></rep:assessment>` → akzeptiert;
2. `<rep:assessment><rep:reject/></rep:assessment>` → abgelehnt;
3. nur bei älteren oder angepassten Berichten ohne Assessment: Prozessrückgabecode als Rückfall;
4. kein valider Bericht → nicht ausgeführt.

Widersprechen sich Bericht und Rückgabecode, wird die XML-Entscheidung verwendet und eine Warnung erzeugt.

## Regel-IDs

| Präfix | Bereich |
|---|---|
| `REQ` | Pflichtangaben |
| `PROFILE` | Profilkennung |
| `CODE`, `CURR`, `ADDR` | Codes, Währungen und Adressen |
| `DATE` | Datumslogik |
| `LINE` | Positionsstruktur |
| `CALC` | Berechnungen |
| `TAX` | Steuern und Steuersemantik |
| `PAY` | Zahlung und Bankdaten |
| `TECH` | technischer Anhang |
| `KOSIT` | offizielle Prüfung und Anbindung |

Regel-IDs sollen nach Veröffentlichung nicht für eine andere Bedeutung wiederverwendet werden.

## Bekannte Grenzen

- keine Prüfung, ob eine Leistung tatsächlich erbracht wurde;
- keine Echtheits- oder Signaturprüfung;
- keine Prüfung von Handelsregister-, Steuer-ID- oder Kontoinhaberdaten gegen externe Register;
- keine vollständige juristische Würdigung von Leistungsort, Steuerbefreiung oder Reverse Charge;
- keine Garantie, dass ein externer Validator dieselben Versionen der Regelartefakte verwendet;
- keine OCR-Interpretation visueller PDFs.
