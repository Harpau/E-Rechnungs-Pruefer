# Stand der Upload- und Workerabsicherung

Stand: 19. September 2026. Die Schutzänderungen für frühe Uploadbegrenzung und begrenzte
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
Fassung bestand das lokale vollständige Gate; der unten dokumentierte C1-Lauf bestätigt inzwischen auch
die vollständige Python-Matrix einschließlich Python 3.13.

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

Am 18. September wurde eine begrenzte Ausnahme ausdrücklich freigegeben: genau ein zusätzlicher manueller
Windows-Diagnoselauf mit den unveränderten C6-Paketen und, nach belegter Ursache sowie unabhängig geprüfter
Korrektur, genau ein vollständiger Bestätigungslauf mit frisch gebauten Paketen. Das ursprüngliche Budget
bleibt mit 2/2 verbraucht; automatische Wiederholungen sind nicht erlaubt. Der Diagnosemodus erfasst ausschließlich
`held-responses` und `health`, bindet Produktbytes und aktuelle Harnessrevision getrennt und erhält den
Produktzustand bei einem Fehler. Ein erfolgreiches Diagnoseergebnis ersetzt keine vollständige Paketabnahme.
Vor dem Dispatch sind der neue lokale Controllerplan und die exakten Hash-/Laufbindungen maßgeblich.

Der zusätzliche [Diagnoselauf 35349999750, Versuch 1](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35349999750)
auf Harnesscommit `ebaba2e8a8f0a76c74979e8fc0871c00b02ee758` endete bereits in der Vorprüfung:
138 Tests bestanden, zwei neue plattformabhängige Tests scheiterten an ZIP-Namensnormalisierung bzw. impliziter
Textcodierung. C6-Download, Installerprobe und Produktkontext wurden nicht gestartet. Das ist `FAIL_HARNESS`;
über den ursprünglichen `PermissionError` liegt kein neuer nativer Befund vor. Die Testkorrektur verwendet
identische rohe ZIP-Namen auf allen Plattformen und explizites UTF-8; die Archivprüfung bewertet zusätzlich
den unveränderten Originalnamen vor einer Windows-Normalisierung.

Nach zusätzlicher ausdrücklicher Freigabe wurde genau ein weiterer
[Diagnoselauf 35354724166, Versuch 1](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35354724166)
auf Harnesscommit `68de5c41a64fa7c83ecdcbc1afa4ac9e98f75564` ausgeführt. Alle 146 Vorprüfungstests bestanden.
Installer und installierte EXE waren hashgleich den ursprünglichen C6-Bytes. Zwei gehaltene 25-MiB-Antworten
bestanden erneut. Die folgende Healthprobe scheiterte nach 2,674 Sekunden in der Phase `observe-input`:
`DuplicateHandle` lieferte Win32-Fehler 5 (`ERROR_ACCESS_DENIED`, `errno=13`).

Im unmittelbar nachfolgenden Snapshot waren Backend, beide Supervisoren und beide Worker über ihre
gebundenen Prozesshandles noch `alive`; ein Worker-Marker war erkannt, beide HTTP-Anfragen noch offen.
Ein bereits vollendetes Prozessende ist damit nicht belegt. Der Snapshot beweist allerdings auch nicht,
dass das konkrete Eingabehandle zum Zeitpunkt des API-Aufrufs noch gültig war. Der Beobachter fordert
`PROCESS_DUP_HANDLE` an und verwendet passende Pointer-Signaturen; tatsächlich gewährte Rechte und der
ursprüngliche native NTSTATUS wurden nicht aufgezeichnet. Eine bestimmte Rechte- oder Handleursache ist
weiter offen. Das Ergebnis bleibt `INCONCLUSIVE`; eine Produktstörung oder Überschreitung der Healthfrist
ist nicht nachgewiesen. Der Dienst und die weiteren Paketfälle wurden nicht ausgeführt.

Der Produktzustand wurde beim Befund erhalten; Installerlog, Fehlerbericht, Kontextreceipt und Originalarchiv
sind gebunden. Deinstallation oder erfolgreicher Produktcleanup sind nicht nachgewiesen. Der spätere Abbau
des temporären GitHub-Runners ersetzt diesen Nachweis nicht.

Der anschließend ausdrücklich freigegebene
[Diagnoselauf D3, 35359989931, Versuch 1](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35359989931)
verwendete Harnesscommit `55fc5944fad738795f6fede2211f4f262483633b` und dieselben C6-Produktbytes.
Die Ergänzung erfasst tatsächlich gewährte Handle-Rechte, feste Zähler und Dauern bestehender Aufrufe sowie
bei Fehlern einen ausdrücklich nur korrelierten Last-NTSTATUS. Sie verändert weder Produktrechte noch
Bestehenskriterien. Die bisher tolerierten Pipe-Endzustände 109/232/233 bleiben unverändert behandelt.
169 Windows-Vorprüfungen bestanden; das vollständige lokale Gate bestand mit 2.708 Tests und zehn Skips.

D3 bestand beide begrenzten Fälle und die reguläre Deinstallation samt vorhandenen Restprüfungen.
Zwei gehaltene 25-MiB-XML-Antworten wurden bytegleich abgerufen. In der anschließenden Healthprobe wurden
beide Worker-Marker erkannt; alle fünf gebundenen Prozesshandles besaßen die angeforderten Rechte
`0x101441`, einschließlich `PROCESS_DUP_HANDLE`. Die Worker lieferten 163 bzw. 156 erfolgreiche
Duplikationen und ebenso viele erfolgreiche Peeks, ohne API-Fehler. Die längste Duplikation dauerte 57,6 µs,
der längste Peek 176,5 µs. Die drei Health-Antworten benötigten 21,95 bis 32,96 ms; die zusätzliche Anfrage
erhielt `503 analysis_capacity_error` nach 3,41 ms. Beide PDF-Antworten erreichten HTTP 200, die gebundenen
Rollen endeten und ein anschließender frischer XML-Auftrag bestand.

Damit lautet der Status dieser beiden D3-Fälle `PASS`. Die Diagnose der früheren Ursache bleibt dagegen
`INCONCLUSIVE`: Es gab keinen fehlgeschlagenen D3-Aufruf, an dem ein Fehlerstatus oder entzogene Rechte
hätten gemessen werden können. Die gewährten Rechte dieses Laufs gelten nicht rückwirkend für D2.
Eine Handle-Race, ein Rechteentzug oder eine erfolgreiche Reparatur ist weiterhin nicht bewiesen.
D3 ersetzt weder die übrigen Desktopfälle noch die Dienst-/Recovery-Abnahme.

Zu diesem Zeitpunkt waren alle drei zusätzlich freigegebenen Diagnoseläufe verbraucht; C1 war ungenutzt
und seine Voraussetzung – belegte Ursache und unabhängig geprüfte Korrektur – nicht erfüllt.

## Vollständiger funktionaler Lauf C1

Nach ausdrücklicher Änderung dieser Voraussetzung wurde genau ein vollständiger funktionaler
[C1-Lauf 35365866430, Versuch 1](https://github.com/Harpau/E-Rechnungs-Pruefer/actions/runs/35365866430)
auf Commit `9fcc02e4f7f318b8660f7d4babbfe7f4f33da69b` ausgeführt. Die ursprüngliche Fehlerursache durfte dabei
offenbleiben; eine Reparatur wurde nicht behauptet. Der neue Commit ergänzt ausschließlich die getrennte
Aufbewahrung des ohnehin gebauten Recovery-Testinstallers samt Regression und Dokumentation. Alle
Produkt-, Rechte-, Frist- und Bestehenskriterien blieben unverändert. Vor Dispatch bestand das vollständige
lokale Gate mit 2.709 Tests, zehn Skips und 86,43 % Coverage; Plan und Änderung wurden unabhängig geprüft.

| C1-Prüfung | Ergebnis und Grenze |
|---|---|
| Quality und Python 3.11–3.14 | Alle Jobs bestanden; auch der frühere Python-3.13-Testtimeout trat nicht erneut auf. |
| Source/Wheel | 79 Appdateien bytegleich; acht Aufträge im außerhalb des Checkouts installierten Wheel, 16 Rollen beendet, keine belegten Plätze. |
| Linux amd64/arm64 | Funktionale Kataloge, Abbruchfälle, HTTP und KoSIT bestanden; beide Jobs scheiterten ausschließlich am strikten OS-Sicherheitsaudit. Imageexport/-upload wurde deshalb übersprungen. |
| macOS arm64 | Native Kataloge und Lebenszyklusprüfungen bestanden; 386 Tests bestanden, ein Windows-spezifischer Skip. |
| Windows vor Installation | 1.651 Tests bestanden, 16 Skips; nativer Katalog, Abbruchfälle, KoSIT und neuer Paketbau bestanden. |
| Installierter Desktop | Zwei gehaltene, bytegleiche 25-MiB-XML-Antworten bestanden. Die folgende Health-Beobachtung brach erneut im Prüfhelfer ab. |
| Weitere Desktopfälle, Modusausschluss und Dienst/Recovery | Nicht erreicht. Keine vollständige Paketabnahme; keine erfolgreiche Produktdeinstallation behauptet. |

Der neue Health-Fehler entstand nach 2,176 Sekunden bei `DuplicateHandle`, erneut Win32=5/errno=13.
Beim betroffenen Worker waren zuvor 392 Duplikationen und 392 Peeks erfolgreich. Die 393. Duplikation
scheiterte nach 20,8 µs. Tatsächlich gewährte Rechte wurden sowohl bei Bindung als auch nach dem Fehler mit
`0x101441` einschließlich `PROCESS_DUP_HANDLE` gemessen. Ein fehlendes Duplikationsrecht am gehaltenen
Quellprozesshandle erklärt diesen C1-Befund daher nicht.

Der unmittelbar erfasste, ausdrücklich nur korrelierte Last-NTSTATUS war `0xC000010A`.
Microsoft bezeichnet ihn als
[`STATUS_PROCESS_IS_TERMINATING`](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55),
also eine Handle-Duplikation aus oder in einen beendenden Prozess. Das passt zu einem zeitlichen Konflikt
zwischen Beobachtung und Prozessende. Dass alle fünf Prozesshandles im nachfolgenden Snapshot noch nicht
signalisiert waren, widerspricht dem nicht: Bei
[`ExitProcess`](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-exitprocess)
liegt die Handleschließung vor dem Prozessobjekt-Signal. Der Last-NTSTATUS ist jedoch kein garantiert dem
fehlgeschlagenen Aufruf zugeordneter direkter Rückgabewert. Worker-Endstatus und beide fertigen HTTP-Ergebnisse
fehlen. Eine bestimmte Abbauursache, ein Produktfehler oder eine erfolgreiche Reparatur bleiben unbewiesen.

Der Desktop-Kindkontext endete `INCONCLUSIVE` und sperrte die nachfolgenden Mutationen. Der Prüfhelfer erhielt
Produktprozesse, Installation und Autostart beim Befund; der spätere Abbau des GitHub-Runners ist kein
Deinstallationsnachweis. Die Originalinstaller, EXEs, der gesonderte Recovery-Testinstaller und die Nachweise
werden getrennt erhalten. Für den Recovery-Testinstaller entstand wegen des früheren Abbruchs kein
Dienst-Kindkontext; er ist ein ungetestetes Buildartefakt und kein Dienst-PASS.

Die aktuellen Container-Scans melden je Architektur 49 OS-Kennungen in 69 Paketzeilen, davon unverändert
acht HIGH-Kennungen in neun Zeilen. Gegenüber C6 neu ist `CVE-2026-8674` (MEDIUM) für `libc-bin` und `libc6`;
keine bisherige Kennung entfiel. Diese Scannerfeststellung ist keine zusätzliche Ausnutzbarkeitsbewertung.
Die Python- und KoSIT-Java-Audits sind ohne Befund. Das Nullbefund-Gate bleibt gesperrt.

C1 ist mit 1/1 verbraucht; ursprüngliche Desktopversuche 2/2 und D1–D3 jeweils 1/1 bleiben unverändert.
Ein weiterer nativer Lauf, pauschaler Retry, Rechteerhöhung oder Fehlerunterdrückung ist nicht freigegeben.
Die technische Gesamtabnahme bleibt `INCONCLUSIVE`; Merge, Tag und Veröffentlichung sind nicht erfolgt.

## Freigegebene Reparatur der Paketbeobachtung

Am 19. September wurde nach drei unabhängigen Planreviews genau ein zusätzlicher vollständiger CI-Lauf
mit wesentlich überarbeitetem Beobachter freigegeben. Alle oben genannten Altbudgets bleiben verbraucht.
Die neue Freigabe enthält keine automatischen Wiederholungen, lokalen VM-Aktionen, Signatur-/Clientabnahme
oder Veröffentlichung.

Die Umsetzung ersetzt fremde Eingabe-Pipe-Zugriffe durch begrenzte, ausdrücklich Bearer-authentifizierte
Ownernachweise. Eingabeannahme, vorbereiteter Operationsaufruf, tatsächlich abgeschlossenes Operationsintervall,
Prozessbereinigung und Antwortversand werden getrennt. Vollständige Zeitüberlappung ist für den Lastnachweis
erforderlich; gezielte Abbrüche behaupten weder einen gesicherten Funktionsbeginn noch CPU-Aktivität beim Kill.
Der genaue Vertrag steht im [Releasegate](RELEASE.md#nachweis-der-installierten-windows-verarbeitung).

Ein neuer nativer Pflichtfallkatalog muss vor jeder Paketinstallation vollständig bestanden sein. Anschließend
werden frische Desktop-, Shipping-Dienst- und separate Recovery-Artefakte gebaut und anhand ihrer jeweiligen
Kontexte bewertet. Vorbereitung und lokale Tests sind noch kein nativer Windows-Paketnachweis. Der zusätzliche
Lauf ist hier noch nicht als begonnen oder bestanden dokumentiert; die historische C1-Ursache bleibt unbewiesen.
