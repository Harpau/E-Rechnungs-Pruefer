# Drittkomponenten

Maßgeblich sind die Lizenztexte der tatsächlich installierten Versionen.

## Laufzeit

| Komponente | Zweck | Projektlizenz |
|---|---|---|
| FastAPI | HTTP-API und Webanwendung | MIT |
| Uvicorn | ASGI-Server | BSD-3-Clause |
| Jinja2 | HTML-Berichte | BSD-3-Clause |
| python-multipart | Multipart-Uploads | Apache-2.0 |
| lxml | XML-Verarbeitung | BSD-artig |
| pypdf | eingebettete PDF-Dateien | BSD-3-Clause |
| ReportLab | Erzeugung eigenständiger PDF-Prüfberichte | BSD-3-Clause |
| Noto Sans und Noto Sans SC | Eingebettete Unicode-Schriften in PDF-Prüfberichten | SIL Open Font License 1.1 |

## Entwicklung

Pytest, pytest-cov, Ruff, Mypy, Build, Twine, pip-audit, Pre-commit, HTTPX und HTTPX2 werden ausschließlich für Entwicklung, Tests und Veröffentlichung eingesetzt.

## Windows-Paket

| Komponente | Zweck | Projektlizenz |
|---|---|---|
| Eclipse Temurin JRE | Java-Laufzeit für die gebündelte KoSIT-Prüfung | GPL-2.0 mit Classpath Exception sowie enthaltene Drittbedingungen |
| Pillow | Erzeugung des Symbols im Windows-Infobereich | MIT-CMU |
| pystray | Windows-Infobereich und Beenden-Menü | LGPL-3.0 |
| pywin32 | Windows-SCM, DACLs und lokale Named-Pipe-IPC | Python Software Foundation License 2.0 |

PyInstaller und Inno Setup werden ausschließlich während des Builds verwendet. Für eine kommerzielle Nutzung von Inno Setup sind die jeweils aktuellen Lizenz- und Erwerbsbedingungen zu prüfen.

## Optionale KoSIT-Komponenten

Der KoSIT-Validator und die Validator-Konfiguration für XRechnung werden im Quell- und Repository-Release nicht mitgeliefert. `scripts/install_kosit.py` lädt sie nach ausdrücklichem Aufruf in das lokale, von Git und diesen Releases ausgeschlossene Verzeichnis `vendor/`. Der Windows-Installer enthält die in `packaging/windows/components.lock.json` festgeschriebenen und beim Build anhand ihrer SHA-256-Prüfsummen verifizierten Komponenten. Lizenz- und NOTICE-Dateien aus den offiziellen Archiven bleiben erhalten, soweit sie dort enthalten sind.

- KoSIT Validator: Apache License 2.0
- Validator Configuration for XRechnung: Apache License 2.0
- eingebundene Schema-, Schematron- und Codelist-Artefakte: Bedingungen der jeweiligen offiziellen Veröffentlichung

Bei einer Weitergabe einer lokal erweiterten Installation müssen die enthaltenen LICENSE- und NOTICE-Dateien dieser Komponenten beachtet werden.
