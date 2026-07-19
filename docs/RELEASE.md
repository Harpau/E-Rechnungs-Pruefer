# Release-Prozess

## 1. Version vorbereiten

Version in folgenden Dateien ändern:

- `VERSION`
- `pyproject.toml`
- `app/__init__.py`
- `USER_AGENT` in `scripts/install_kosit.py`

Anschließend `CHANGELOG.md` aktualisieren.

```sh
python scripts/verify_version.py
```

## 2. Qualitätsprüfung

```sh
./scripts/check.sh
python -m pip_audit
```

Zusätzlich die Anwendung mit den anonymisierten CII-/UBL-Beispielen und einer Hybrid-PDF manuell öffnen. Bei KoSIT-Änderungen mindestens einen Annahme-, Ablehnungs- und Startfehlerfall prüfen.

## 3. Artefakte bauen

```sh
python scripts/build_release.py
```

`dist/` enthält:

- `E-Rechnungs-Pruefer-<Version>-Codex-GitHub.zip`
- Wheel
- Source Distribution
- `E-Rechnungs-Pruefer-<Version>-SHA256SUMS.txt`

Der Repository-Build schließt `.git`, virtuelle Umgebungen, lokale `.env`-Dateien, KoSIT-Dateien, PDFs, Schlüsselmaterial, Berichte und nicht freigegebene XML-Dateien aus.

## 4. Artefakte prüfen

```sh
unzip -l dist/E-Rechnungs-Pruefer-*-Codex-GitHub.zip
python -m twine check dist/*
```

Empfohlen ist außerdem ein Installationstest in einer neuen virtuellen Umgebung:

```sh
python -m venv /tmp/einvoice-release-test
/tmp/einvoice-release-test/bin/python -m pip install dist/*.whl
/tmp/einvoice-release-test/bin/python -c "import app; print(app.__version__)"
```

## 5. Tag und GitHub Release

```sh
git tag -a vX.Y.Z -m "E-Rechnungs-Pruefer X.Y.Z"
git push origin vX.Y.Z
```

Der Release-Workflow wiederholt Check und Build, verifiziert die Tag-Version und veröffentlicht die Dateien aus `dist/`.

## Rücknahme

Bei einem fehlerhaften Release keine vorhandenen Artefakte still ersetzen. Release als fehlerhaft kennzeichnen, neuen Patch erstellen und im Changelog transparent beschreiben.
