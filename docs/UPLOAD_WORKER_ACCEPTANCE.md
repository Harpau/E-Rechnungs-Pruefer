# Stand der Upload- und Workerabsicherung

Stand: 17. September 2026. Die Schutzänderungen für frühe Uploadbegrenzung und begrenzte
Rechnungsverarbeitung sind implementiert. **Die technische Gesamtabnahme ist `INCONCLUSIVE`; der PR bleibt
Entwurf und ist nicht zur Veröffentlichung freigegeben.** Die vollständigen Nachweise für den installierten
Windows-Desktop und -Dienst fehlen. Zusätzlich bleibt das OS-Sicherheitsgate gesperrt.

Maßgeblich sind der lokale Controllerplan und die versiegelten Originalnachweise nach
[`ACCEPTANCE.md`](ACCEPTANCE.md). Dieses Dokument beschreibt den belegten Stand und ersetzt diese Evidence nicht.

## Implementierte Schutzwirkung

Die vier Uploadendpunkte verwenden dieselbe begrenzte RAM-Annahme und zwei gemeinsame Auftragsplätze.
Authentifizierung, Headerprüfung und Kapazitätsprüfung erfolgen vor dem Upload. Pro Anfrage wird genau eine
Datei bis 25 MiB angenommen; zusätzliche Multipartdaten, Fristen und Ausgaben sind ebenfalls begrenzt.
Es entstehen keine Upload-Spooldateien. Ein Platz bleibt bis zum nachgewiesenen Prozessende und dem Ende des
Antwortversands belegt. `/api/xml` unterliegt denselben Grenzen und erhält die ursprünglichen XML-Bytes.

Analyse, PDF-Extraktion und Berichtserstellung laufen in eigenen Prozessen mit nativen Zeit- und Speichergrenzen.
Technische Abbrüche werden als technische Fehler ausgewiesen. Sie erzeugen keine offizielle KoSIT-Ablehnung.
Die Einzelgrenzen und der HTTP-Vertrag stehen im [`Sicherheitsmodell`](SECURITY_MODEL.md#ressourcenverbrauch).

Diese Änderung begrenzt Ressourcenverbrauch und Prozesslebensdauer. Sie ist keine vollständige Sandbox gegen
Codeausführung und ergänzt keine allgemeine Dateisystem- oder Netzwerkisolation. Bestehende CVEs werden dadurch
nicht geschlossen oder vom Sicherheitsgate ausgenommen.

## Kandidat und Nachweise

Die folgenden nativen Ergebnisse gehören ausschließlich zum Commit
`630444b57e81d524a61b128abe78ce68278719b5`, CI-Merge
`48b2be57eba3b40cf75c732f35cc80d6e96f507a`,
[CI-Lauf 35263705992, Versuch 1](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35263705992).
Spätere Dokumentations- oder Testkorrekturen erhalten dadurch keine neue native Artefaktabnahme.

| Prüfung | Ergebnis und Aussagegrenze |
|---|---|
| Lokales vollständiges Gate nach der Testfristkorrektur | Versionen, Ruff, Format, Mypy, Actionlint und 2.615 Tests bestanden; 10 übersprungen, 86,43 % App-Coverage. Lokales Python 3.14.6 auf macOS Intel; kein Ersatz für Windows. |
| CI-Qualitätsgate und installiertes Wheel | Bestanden; 79 Appdateien in Repository-ZIP, sdist und Wheel bytegleich. Acht echte Wheel-Aufträge außerhalb des Checkouts, 16 Linux-Rollen beendet, keine belegten Plätze. |
| Python-Matrix | 3.11, 3.12 und 3.14 bestanden. 3.13: 2.609 bestanden, 15 übersprungen; ein externer PowerShell-Testprozess überschritt seine 10-s-Testfrist. |
| Linux amd64 und arm64 | Je 15 Katalogaufträge, fünf aktive Abbruchfälle und vier echte KoSIT-Fälle bestanden. HTTP-/Schema-/PDF-/Original-XML-Prüfungen bestanden. Containerjobs bleiben wegen OS-Befunden rot. |
| macOS arm64 | 15 Katalogaufträge mit 45 Rollen, sechs Abbruchfälle sowie begrenzte Heap-/Adressraum- und Wächterstartproben bestanden; 386 Tests bestanden, einer übersprungen. |
| Windows x64 im Quellbetrieb | 1.616 Tests bestanden, 16 übersprungen; Prozesskatalog, aktive Abbruchfälle und vier echte KoSIT-Fälle bestanden. |
| Windows-Paketbau | Unsigned Installer und drei EXEs gebaut und als Originale erhalten; Hashbindungen und CPython-Sicherheitsreceipts geprüft. Das belegt keine vollständige Dienstabnahme. |
| Installierter Windows-Desktop | Zwei gehaltene 25-MiB-XML-Antworten bestanden. Anschließende Health-/Rollenbeobachtung scheiterte im Prüfhelfer; Gesamtstatus `INCONCLUSIVE`. |
| Installierter Windows-Dienst und spätere Paketfälle | Wegen des vorherigen Abbruchs nicht erreicht; keine aktuelle Abnahme. |
| Dependency Audit / CodeQL | 24 OS-/Python-/Profilkombinationen und die Python-Lockaudits bestanden; CodeQL erfolgreich. Das übergreifende Dependency-Gate bleibt wegen der Containerbefunde rot. |

Die PowerShell-Korrektur erweitert ausschließlich die äußere Frist des reinen Resolver-Regressionshelfers
von 10 auf 30 Sekunden. Assertions und Produktfristen bleiben unverändert. Ein langsamer PowerShell-/NET-Start
ist eine mögliche Erklärung; die genaue Ursache des CI-Timeouts wurde nicht aufgezeichnet. Die korrigierte
Fassung bestand das lokale vollständige Gate; ein erneuter Linux-CI-Nachweis steht aus.

## Ressourcenprofil und Kalibrierung

MiB und GiB bezeichnen binäre Einheiten. Die folgenden festen Profile werden vor Rechnungsinput gebunden.
Das Limit wächst nicht mit dem eingehenden Dokument.

| Plattform | Worker: Import → Verarbeitung | Supervisor | Java einschließlich nativer Anteile | Durchsetzung |
|---|---:|---:|---:|---|
| Linux | 2.048 → 768 MiB | 512 MiB | 4.096 MiB, Heap 512 MiB | `RLIMIT_AS`: gemessene, geprüfte Rollenbasis plus fester Zuschlag |
| macOS | 4.096 → 1.536 MiB | 1.024 MiB | 4.096 MiB, Heap 512 MiB | `RLIMIT_AS`: Basis plus Zuschlag; Wächter zusätzlich 64 MiB |
| Windows | 2.048 → 768 MiB | 512 MiB | Java-Job 4.096 MiB für Launcher und JVM, Heap 512 MiB | Private-Commit-Limits verschachtelter Jobs; äußerer Auftragsjob 6,5 GiB |

Die maximal zugelassene POSIX-Basis beträgt unter Linux 1 GiB, unter macOS Intel 64 GiB und unter macOS ARM
512 GiB. Diese Wächtergrenze ist kein zusätzlich verfügbares RAM. Die macOS-ARM-Probe maß etwa 392 GiB virtuellen
Adressraum; getrennte, begrenzte Heap- und mmap-Proben bestätigten dort die Limitdurchsetzung.

| Native C6-Probe | Worker-Spitze | Supervisor-Spitze | Echte Java-Probe | READY im synthetischen Katalog |
|---|---:|---:|---:|---:|
| Linux amd64 | 152,28 MiB RSS | 85,69 MiB RSS | 460,37 MiB RSS | 1,254–1,474 s |
| Linux arm64 | 151,26 MiB RSS | 84,70 MiB RSS | 440,61 MiB RSS | 1,546–1,654 s |
| macOS arm64 | 151,63 MiB RSS | 26,38 MiB RSS | Nicht Bestandteil dieses C6-Mac-Katalogs | 0,354–0,558 s |
| Windows x64, Quellkatalog | 140,59 MiB Working Set | 22,49 MiB Working Set | 493,55 MiB privater Job-Commit, einschließlich JVM | 0,567–0,750 s |

Die Werte sind Maxima unterschiedlicher Aufträge und Messgrößen, keine gleichzeitig gemessene Gesamtsumme.
Für die gesonderten vier Windows-Javafälle erreichte der Worker 59,66 MiB Working Set / 49,58 MiB privaten
Commit, der Supervisor 24,64 / 19,01 MiB. Der äußere Job erreichte 557,54 MiB privaten Commit und enthält den
Javajob bereits. Alle 16 ursprünglichen Jobhandles bestätigten beim normalen Schließen null aktive Prozesse.
Die Lebenszeitspitzen können auch die Importphase enthalten. Der kleine Java-Launcher-Prozess allein ist kein
Nachweis über den Speicherverbrauch der JVM.

Bei zwei Aufträgen mit Java ergeben die festen POSIX-Zuschläge rechnerisch unter Linux höchstens 13 GiB beim
Import bzw. 10,5 GiB während der Verarbeitung, jeweils zuzüglich sechs Rollenbasen. Auf macOS sind es
18,125 bzw. 13,125 GiB zuzüglich acht Rollenbasen einschließlich der Wächter. Das sind Summen virtueller
Adressraumlimits verschiedener Prozesse, keine RAM-Reservierung. Eine physische Speicherreserve kann aus
`AS-Limit minus RSS` nicht berechnet werden.

Unter Windows begrenzen die beiden äußeren Jobs den privaten Commit zusammen auf höchstens 13 GiB. Die
inneren Verarbeitungsbudgets summieren sich auf 10,5 GiB und liegen **innerhalb** dieser äußeren Grenze;
beide Summen dürfen nicht addiert werden. Der HTTP-Backendprozess ist darin nicht enthalten. Diese Grenzen
gelten pro Backendinstanz; mehrere Instanzen vervielfachen den Bedarf.

Für den Backendprozess ergibt eine konservative reine Payloadrechnung aus zwei Uploads und zwei maximalen
JSON-/HTML-Antworten `2 × (25 + 128) = 306 MiB`. Interpreter, Bibliotheken, IPC-, HTTP- und Kernelpuffer kommen
hinzu. Das ist weder eine gemessene Speicherspitze noch eine harte RSS-Quote. Der native Versuch unten prüfte
zwei 25-MiB-XML-Antworten; maximal große JSON-/HTML-Antworten und deren Reserven bleiben damit unbewiesen.

### Gehaltene Antworten im installierten Desktop

Zwei HTTP-200-Antworten mit jeweils exakt 26.214.400 Byte wurden zunächst nicht gelesen. Vor der Messung
waren ihre vier Verarbeitungsrollen beendet. Eine zusätzliche Anfrage erhielt unmittelbar vor und nach der
Messung `503 analysis_capacity_error`. Drei Healthanfragen antworteten innerhalb von 37 ms. Beide XMLs wurden
anschließend im gemeinsamen Drainfenster bytegleich abgerufen; danach gelang ein frischer XML-Auftrag.

| Messpunkt desselben gebundenen Backendprozesses | Working Set | Privater Commit |
|---|---:|---:|
| Vorher | 88,01 MiB | 69,98 MiB |
| Beide Antworten gehalten | 138,41 MiB | 120,02 MiB |
| Nach Abruf und Folgeauftrag | 89,50 MiB | 72,75 MiB |

Die Payload umfasst insgesamt 50 MiB. Daraus folgt nicht, dass exakt 50 MiB vollständig im Parent-RAM liegen;
auch Transportpuffer enthalten Daten. Diese Messung belegt weder zwei 128-MiB-Antworten noch den Dienstmodus.

## Offene Abnahme und erlaubte Fortsetzung

Der nachfolgende Desktop-Prüfhelfer brach während der Rollen-/Pipe-Beobachtung nach etwa 2,51 Sekunden mit
`PermissionError` ab. Der genaue Windows-Aufruf und numerische Fehlercode wurden nicht erhalten. Eine
Handle-Race ist unbewiesen; ein konkreter Produktfehler oder eine überschrittene Healthfrist folgt daraus nicht.
Die Klassifikation lautet `FAIL_HARNESS`, das betroffene Gesamtszenario `INCONCLUSIVE`.

Zusammen mit dem vorherigen Fehler der Python-Pfadauswahl sind die zwei vorab gebundenen automatischen
Desktopversuche ausgeschöpft. Ein eigenes neues Health-Budget war nicht vorgesehen. Nach
[`ACCEPTANCE.md`](ACCEPTANCE.md#wiederholung-und-snapshot-neustart) darf jetzt kein weiteres Ad-hoc-Harness
gebaut und kein automatischer Desktopversuch durch Umbenennung des Szenarios oder Kandidaten gestartet werden.
Der vorab festgelegte Fallback ist `INCONCLUSIVE`. Cleanup-/Restoreaktionen wurden nach dem Befund nicht
ausgeführt; ein erfolgreicher Produktcleanup darf nicht behauptet werden.

Die Originalartefakte, Rohlogs, Receipts und früheren Kandidaten bleiben lokal erhalten. Eine externe,
verschlüsselte Archivierung wird nicht behauptet. Der Abschlusscommit enthält nur die geprüfte Testfrist und
Dokumentation. Sein `[skip ci]` verhindert eine unbeabsichtigte neue PR-Abnahme; er erzeugt keinen grünen
CI-Nachweis und darf nicht zum Umgehen eines Mergegates verwendet werden.

Die nächste Entscheidung muss den offenen Windows-Abnahmeweg ausdrücklich klären. Eine Fortsetzung benötigt
einen zulässigen, neu gebundenen Weg nach den Abnahmeregeln oder eine ausdrücklich geänderte Vorgabe; allgemeine
Fortsetzungsaufträge setzen das Fehlerbudget nicht zurück. Bis dahin bleiben die vollständige Desktop-/Dienst-
und Ressourcenabnahme offen. Die 48 unveränderten OS-Sicherheitskennungen (67 Paketzuordnungen, darunter acht
HIGH-Kennungen in neun Paketzeilen je geprüftem Container) sind ein weiterer Freigabeblocker. Merge, Tag und
Veröffentlichung sind nicht erfolgt.
