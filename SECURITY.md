# Sicherheit

## Unterstützte Versionen

Sicherheitskorrekturen werden für die aktuelle Minor-Version gepflegt. Für ältere Versionen sollte zunächst auf die aktuelle Version aktualisiert und der Fehler dort reproduziert werden.

## Schwachstellen melden

Sicherheitsrelevante Probleme bitte vertraulich über GitHub Private Vulnerability Reporting oder eine im Repository hinterlegte Sicherheitskontaktadresse melden. Keine echten Rechnungen, Kontodaten, Steuerkennungen oder personenbezogenen Daten mitsenden; eine synthetische Reproduktion genügt.

Eine gute Meldung enthält Auswirkung, Angriffsvoraussetzungen, betroffene Version, minimale Reproduktion und eine mögliche Abhilfe.

## Sicherheitsannahmen

Die unkonfigurierte Quellinstallation ist für den lokalen Einzelplatzbetrieb auf `127.0.0.1` vorgesehen und
stellt keine Authentifizierung oder Mandantentrennung bereit. Wird im Quellbetrieb `EINVOICE_API_TOKEN` gesetzt,
schützt es die fachlichen `/api/*`-Endpunkte; der interaktive Start richtet für die Browseroberfläche eine davon
getrennte Sitzung ein. Die installierten Windows-Betriebsarten verwenden dort immer ein persistentes
Bearer-Token und für die Browseroberfläche eine kurzlebige lokale Sitzung. `/api/health` bleibt in beiden Fällen
als lokaler Healthcheck tokenfrei. Keine dieser Betriebsarten ist ohne zusätzliche Maßnahmen für ein öffentliches
Netzwerk gedacht.

## Schutzmaßnahmen

- Uploads werden standardmäßig nicht dauerhaft gespeichert.
- XML-DTDs und ENTITY-Deklarationen werden abgewiesen.
- Externe Entitäten, DTD-Nachladung und XML-Netzwerkzugriffe sind deaktiviert.
- Upload- und technische Darstellungsgrößen sind begrenzt.
- KoSIT läuft mit Zeitlimit in einem temporären Verzeichnis.
- Java-/Konfigurationsfehler werden nicht als fachliche Ablehnung interpretiert.
- Download-Dateinamen werden bereinigt.
- Browserantworten enthalten restriktive Sicherheitsheader.
- Windows-Desktop- und Dienstmodus trennen das persistente API-Token von der kurzlebigen Browsersitzung und
  prüfen Host sowie Origin; der Dienst übergibt die Sitzung über authentifizierte lokale IPC.
- Das Docker-Image verwendet einen nicht privilegierten Benutzer.
- Quell- und Repository-ZIPs schließen `.env` und `.env.*` (außer der Vorlage `.env.example`), `vendor`,
  gebündelte Java-Laufzeiten, Download-Caches, Schlüsselmaterial, PDFs und nicht freigegebene XML-Dateien aus.
  Das getrennte Windows-Binaries-ZIP enthält ausschließlich die festgeschriebenen und beim Build geprüften
  Laufzeiten und Komponenten.

## Netzwerkbetrieb

Für einen Mehrbenutzer- oder Internetbetrieb sind mindestens TLS, Authentifizierung, Autorisierung, Rate Limits, sichere und datensparsame Protokollierung, Malware-Prüfung, Ressourcenlimits, Prozessisolierung und ein gehärteter Reverse Proxy erforderlich. Rechnungsinhalte und KoSIT-Berichte können personenbezogene und bankbezogene Daten enthalten.

Das detaillierte Bedrohungsmodell steht in `docs/SECURITY_MODEL.md`.
