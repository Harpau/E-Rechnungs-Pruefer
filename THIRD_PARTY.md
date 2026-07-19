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

## Entwicklung

Pytest, pytest-cov, Ruff, Mypy, Build, Twine, pip-audit, Pre-commit, HTTPX und HTTPX2 werden ausschließlich für Entwicklung, Tests und Veröffentlichung eingesetzt.

## Optionale KoSIT-Komponenten

Der KoSIT-Validator und die Validator-Konfiguration für XRechnung werden nicht mitgeliefert. `scripts/install_kosit.py` lädt sie nach ausdrücklichem Aufruf in das lokale, von Git und Releases ausgeschlossene Verzeichnis `vendor/`.

- KoSIT Validator: Apache License 2.0
- Validator Configuration for XRechnung: Apache License 2.0
- eingebundene Schema-, Schematron- und Codelist-Artefakte: Bedingungen der jeweiligen offiziellen Veröffentlichung

Bei einer Weitergabe einer lokal erweiterten Installation müssen die enthaltenen LICENSE- und NOTICE-Dateien dieser Komponenten beachtet werden.
