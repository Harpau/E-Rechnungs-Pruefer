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

Der Repository-Build schließt `.git`, virtuelle Umgebungen, lokale `.env`-Dateien, KoSIT-Dateien, gebündelte Java-Laufzeiten, Download-Caches, PDFs, Schlüsselmaterial, Berichte und nicht freigegebene XML-Dateien aus.

### Windows-x64-Installer

Der Windows-Build läuft nativ auf Windows und ist in [`WINDOWS_PACKAGE.md`](WINDOWS_PACKAGE.md) beschrieben:

```powershell
python scripts\prepare_windows_components.py
.\scripts\build_windows.ps1
.\scripts\test_windows_package.ps1
```

Zusätzliche Artefakte:

- `E-Rechnungs-Pruefer-<Version>-Windows-x64-Setup.exe`
- `E-Rechnungs-Pruefer-<Version>-Windows-x64-SHA256.txt`

Ein reduzierter Build mit `-WithoutOfficialValidation` ist nur ein Entwicklungsartefakt. Vor einem Endbenutzerrelease müssen Java, KoSIT und XRechnung aus `components.lock.json` eingebunden und durch den installierten Pakettest ausgeführt worden sein.

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

Der Release-Workflow wiederholt Check und Build, verifiziert die Tag-Version und veröffentlicht die Dateien aus `dist/`. Der Windows-Installer wird nur dann an den öffentlichen GitHub Release angehängt, wenn seine Authenticode-Signatur gültig ist. Fehlen Azure-Anmeldung, Key-Vault-Konfiguration oder gültige Signatur, schlägt der Release fehl.

Vor dem ersten öffentlichen Tag ist der Workflow `Release` manuell auf `main` zu starten. Dieser Lauf verwendet die geschützte Umgebung `release`, signiert Anwendung und Installer über Azure Key Vault und stellt das Ergebnis ausschließlich als internes Actions-Artefakt bereit. Das Artefakt ist auf einer sauberen Windows-11-x64-Installation zu prüfen; der manuelle Lauf erzeugt keinen GitHub Release.

## Rücknahme

Bei einem fehlerhaften Release keine vorhandenen Artefakte still ersetzen. Release als fehlerhaft kennzeichnen, neuen Patch erstellen und im Changelog transparent beschreiben.
