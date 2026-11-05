# Roadmap

Diese Liste ist unverbindlich und priorisiert nachvollziehbare, getestete Erweiterungen.

## Mit Version 2.0.0 abgeschlossen

- geschlossenes, dokumentiertes Analyseschema 2 mit stabilen Modelltypen und drei getrennten Bewertungsachsen
- strukturierte Erfassung und Darstellung eingebetteter Dateien, belegender Dokumente und fachlicher Referenzen
- versionierte Dokumenttyp-, Profil- und Zahlungsrollensemantik einschließlich Eigenabrechnungen und Gutschriften
- gemeinsamer Präsentationsvertrag für Browser-, HTML- und PDF-Berichte

## Mit Version 2.1.0 abgeschlossen

- offizielle Prüfung von Factur-X 1.09.2 EXTENDED über einen an Szenario und Dokument gebundenen KoSIT-Lauf, mit
  exakter Auswahl der Profilkennung, hashgebundenen Regelartefakten und ausgewiesenem Regelstand
- Sperrdateien mit Dateisatz-Komponenten, Installer und Windows-Vorbereitung mit Sicherung und Rücknahme
- Integrationssuite mit echtem KoSIT-Validator in CI und in den installierten Windows-Pakettests

## Mit Version 2.2.0 abgeschlossen

- vollständige Szenario- und Hashbindung des KoSIT-Berichts auch für XRechnung 3.0.2 und EN 16931 (Folgeaufgabe zur
  Entscheidung E5 der Extended-Planung): die elf Szenarien der Validator-Konfiguration 2026-08-31 stehen im
  Regelsatzregister, die Auswahl liest CII-Guideline-ID und UBL-`CustomizationID` exakt, Szenariodatei und die 25
  daraus geladenen Artefakte sind hashgebunden, Konsolen-Rückfälle entfallen, und Kennungen ohne Szenario lösen keinen
  ungebundenen Lauf mehr aus; Integrationssuite `tests/test_kosit_xrechnung_integration.py` in CI und in den
  Windows-Pakettests

## Kurzfristig

- Übernahme von Factur-X 1.10 / ZUGFeRD 2.6, sobald veröffentlicht: neuer Dateisatz in beiden Sperrdateien und in
  `app/official_rulesets.py`, Aktualisierung der Regeln mit bekannter Abweichung (`KOSIT-RULE-NOTICE`) und der Dokumentation
- weitere anonymisierte Steuer- und Gutschriftregressionen
- bessere UI-Filter für KoSIT- und interne Befunde

## Mittelfristig

- zusätzliche Peppol- und XRechnung-spezifische interne Hinweise
- weitere Factur-X-/ZUGFeRD-Profile (BASIC, BASIC WL, MINIMUM) als zusätzliche Zeilen des Regelsatzregisters mit
  eigenen gebundenen Artefakten; die ZUGFeRD-2.0-Kennung bleibt ausgeschlossen
- optionaler Batch-Modus ohne dauerhafte Speicherung
- barriereärmere Tastatur- und Screenreader-Bedienung
- reproduzierbare Container- und SBOM-Artefakte
- mehrsprachige Darstellungslabels bei unverändertem Rohcode

## Bewusste Nicht-Ziele

- OCR-Rekonstruktion von Scan-Rechnungen
- automatische steuerrechtliche Entscheidung ohne Geschäftskontext
- öffentliche Mehrmandantenplattform ohne eigenes Sicherheits- und Berechtigungskonzept
- Veränderung oder „Reparatur“ der hochgeladenen Original-XML
