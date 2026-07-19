# Sicherheitsmodell

## Schutzgüter

- Rechnungsinhalte und personenbezogene Daten
- Bank- und Steuerkennungen
- Original-XML und Prüfergebnisse
- lokales Dateisystem und Prozessumgebung
- Integrität der KoSIT-Konfiguration

## Vertrauensgrenzen

1. Uploads sind vollständig untrusted.
2. PDF-Anhänge und XML-Namen sind untrusted.
3. XML-Inhalte, Namespaces, Attribute und Textwerte sind untrusted.
4. Java-/KoSIT-Ausgaben und Berichtsdateien sind untrusted, bis sie sicher geparst wurden.
5. Browserausgabe muss alle Rechnungswerte escapen.
6. Downloads aus dem KoSIT-Installer erfolgen nur nach ausdrücklichem Benutzeraufruf.

## Wesentliche Bedrohungen und Kontrollen

### XML External Entity und DTD

Kontrollen: Vorabprüfung auf DTD/ENTITY, `resolve_entities=False`, `load_dtd=False`, `no_network=True` und keine Recovery-/Huge-Tree-Modi.

### ZIP Slip bei XRechnung-Konfiguration

Der Installer prüft jeden ZIP-Zielpfad vor dem Extrahieren gegen das Zielverzeichnis.

### PDF-Anhangsauswahl

Kennwortgeschützte PDFs werden abgelehnt. Verschlüsselte PDFs, die sich mit einem leeren Passwort entschlüsseln lassen, dürfen verarbeitet werden. Es werden nur Anhänge verarbeitet, deren Bytes wie XML aussehen. Bekannte Rechnungsnamen erhalten Priorität. Andere Anhänge werden nur als Metadaten aufgeführt und nicht ausgeführt.

### Ressourcenverbrauch

Uploadgröße, technische Zeilenanzahl und KoSIT-Laufzeit sind begrenzt. Bei Hybrid-PDFs gilt `MAX_UPLOAD_BYTES` sowohl für die ausgewählte Rechnungs-XML als auch für die Summe der dekodierten Anhänge; zusätzlich werden höchstens 100 eingebettete Dateien verarbeitet. Das Dekompressionslimit von pypdf 6 bildet eine weitere Obergrenze. Da solche Prüfungen nicht jede Speicherallokation vor dem Dekodieren verhindern können, sind für den Netzwerkbetrieb weiterhin Prozess-, Speicher- und Parallelitätslimits notwendig.

### Cross-Site Scripting

Jinja2 escaped standardmäßig; die JavaScript-Oberfläche verwendet `escapeHtml` für Rechnungswerte. Änderungen an `innerHTML` müssen sicherstellen, dass jeder untrusted Wert vorab escaped wird. Die Content Security Policy verhindert fremde Skripte und Objekte.

### Pfad- und Dateinamenmanipulation

Upload- und Downloadnamen werden mit `Path(...).name` und einer Zeichen-Whitelist bereinigt. Temporäre KoSIT-Dateien bleiben unter einem neu angelegten Verzeichnis.

### Falsche Validierungsentscheidung

Ein Prozessfehler ohne validen VARL-Bericht ist kein Rechnungsurteil. Eine vorhandene `accept`/`reject`-Entscheidung im Bericht ist maßgeblich und wird gegen den Rückgabecode plausibilisiert.

### Geheimnisse und echte Rechnungen im Repository

`.gitignore`, Release-Filter und `AGENTS.md` schließen lokale Konfigurationen, KoSIT-Dateien, PDFs, Schlüssel und nicht freigegebene XML-Dateien aus. Die Schutzwirkung ersetzt keine Review von `git status` und Release-Inhalten.

## Nicht abgedeckt

- Authentifizierung oder Mandantentrennung
- Malware-Scanning beliebiger PDF-Inhalte
- digitale Signaturprüfung
- Hardware-Isolation des Java-Prozesses
- Schutz gegen einen bereits kompromittierten lokalen Rechner
- rechtssichere Langzeitarchivierung
