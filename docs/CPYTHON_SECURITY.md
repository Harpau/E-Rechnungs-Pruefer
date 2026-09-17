# CPython-Sicherheitsbackport

Die ausgelieferten Docker- und Windows-Laufzeiten verwenden CPython 3.14.7 mit dem
gezielten Upstream-Fix für **CVE-2026-15806**. Der Interpreter behält seine
Upstream-Version; er wird ausdrücklich als Release mit Sicherheitsbackport
dokumentiert. Dies ist keine Aussage über sämtliche übrigen Interpreter-Advisories.

Der [Upstream-Commit a0d023f](https://github.com/python/cpython/commit/a0d023fbd23773e24b35d8368789470e22cda5d8)
bindet gespeicherte urllib-Zugangsdaten zusätzlich an das URL-Schema. Für HTTPS
gespeicherte Zugangsdaten dürfen beim entsprechenden HTTP-Ziel nicht verwendet
werden, auch nicht durch vorab gesendete Basic-Authentication. Bewusst ohne Schema
registrierte Ziele sowie die vorhandenen öffentlichen Hilfsmethoden bleiben
kompatibel. Regressionen reproduzieren den Fehler mit synthetischen Zugangsdaten
am Original und bestehen mit den korrigierten Bytes; sie öffnen keine Verbindung.

## Reproduzierbarer Eingriff

`packaging/python/cpython-security.json` bindet Originaldatei, korrigierte Datei,
Patch und Upstream-Commit. `scripts/cpython_security.py` akzeptiert ausschließlich
die bekannten CPython-3.14.7-Bytes, mit vollständig einheitlichen LF- oder
CRLF-Zeilenenden. Es gibt keine unscharfe Patchanwendung. Schon gepatchte oder
anderweitig veränderte Eingaben werden zurückgewiesen. Die unveränderte
CPython-Lizenz liegt neben dem Patch.

Docker patcht ausschließlich die eigene Builder-Laufzeit vor der Erstellung des
finalen Dateisystems. Das Manifest unterscheidet die korrigierte Datei ausdrücklich
von unveränderten Dateien des offiziellen Basisimages. Das finale Image enthält
den gebundenen Patchnachweis; die originale verwundbare Sicherungsdatei verbleibt
in der Build-Stufe. Beide nativen Architekturen prüfen die tatsächlich geladene
Datei und das korrigierte Verhalten zusätzlich zur vollständigen Paketabdeckung.

Windows- und Source-Builds kopieren zuerst den festgelegten Interpreter in ein
neues privates Verzeichnis. Erst nach Patch- und Verhaltensprüfung wird daraus
die Build-venv erstellt. Eine venv allein isoliert die Standardbibliothek nicht;
die gemeinsam genutzte setup-python-Installation und Benutzerinterpreter werden
deshalb nicht verändert. Die 53-/97-Paket-Locks behalten ihre Versionen und
Wheelhashes. Native Builds müssen die erfolgreiche Relokalisierung beweisen.

Der Windows-Builder lehnt eine ungeprüfte Laufzeit vor Bereinigung alter
Buildausgaben ab. Anschließend werden die `urllib.request`-Codeobjekte aus allen
drei erzeugten EXEs gelesen, vollständig gegen die korrigierte Quelldatei
verglichen und mit denselben Sicherheitsregressionen geprüft. Nur der
maschinenabhängige `co_filename` wird beim Codevergleich ausgenommen. Die
EXE-Prüfung führt keine Installation und keinen EXE-Start aus. Die Belege entstehen
vor einer eventuellen Signierung; spätere signierte Artefakte benötigen weiterhin
ihre regulären finalen Hash- und Signaturnachweise.

## Quellinstallationen und Entfernung des Backports

Wheel und Source-Archiv enthalten keinen Python-Interpreter. Eine Installation
dieser Anwendung aktualisiert oder patcht daher keine systemweite Python-Laufzeit.
Auch die Kompatibilitätstests für Python 3.11–3.14 sind kein Beleg für einen
Sicherheitsbackport in diesen fremden Interpretern. Betreiber von Quellinstallationen
benötigen einen vom Anbieter korrigierten Interpreter. Der enthaltene Patchhelfer
ist ausschließlich für die exakt gebundene Version 3.14.7 vorgesehen.

Sobald ein geeigneter veröffentlichter Herstellerstand den Fix nachweislich
enthält, werden Interpreterbasis und Nachweise gemeinsam aktualisiert. Der alte
Patch darf nicht ungeprüft weiter angewandt oder nur anhand einer Versionsnummer
entfernt werden. Regressionen bleiben bestehen; alle betroffenen Laufzeiten und
eingefrorenen Programme werden neu geprüft. Ein behobener Interpreterbefund
ersetzt weder die übrigen OS-/Paket-Audits noch die Release-Abnahme.
