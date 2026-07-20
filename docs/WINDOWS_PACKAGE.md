# Windows-x64-Paket

## Ziel und Artefakte

Das Endbenutzerpaket läuft auf Windows x64 ohne separat installiertes Python, Java oder KoSIT. Es wird als benutzerbezogener Inno-Setup-Installer ohne Administratoranforderung erzeugt:

- `E-Rechnungs-Pruefer-<Version>-Windows-x64-Setup.exe`
- `E-Rechnungs-Pruefer-<Version>-Windows-x64-SHA256.txt`

Der Installer legt die Anwendung standardmäßig unter `%LOCALAPPDATA%\Programs\E-Rechnungs-Pruefer` ab und erstellt einen Startmenüeintrag. Ein Desktopsymbol ist optional. Windows ARM64 ist kein Ziel dieses Pakets.

## Laufzeitverhalten

`app/windows_launcher.py` reserviert einen freien Port auf `127.0.0.1`, startet Uvicorn mit einem bereits gebundenen Socket und öffnet den Standardbrowser erst nach erfolgreichem Healthcheck. Ein Windows-Mutex verhindert mehrere Serverinstanzen. Beim zweiten Start wird die vorhandene Sitzung erneut im Browser geöffnet. Das Symbol im Windows-Infobereich bietet „Öffnen“ und „Beenden“ an.

Der Desktop-Modus erzeugt pro Start ein zufälliges Token. Der einmalige Startlink setzt ein `HttpOnly`-/`SameSite=Strict`-Cookie und leitet auf die tokenfreie Startseite um. Danach werden Host, Sitzung und bei schreibenden Browseranfragen der Origin geprüft. Ohne Desktop-Umgebungsvariablen bleibt das bestehende Verhalten für Entwicklung, Docker und API-Aufrufe unverändert.

## Gesperrte Prüfkomponenten

`packaging/windows/components.lock.json` legt Downloadquelle und SHA-256-Prüfsumme fest für:

- Eclipse Temurin JRE für Windows x64;
- das ausführbare KoSIT-Standalone-JAR;
- die XRechnung-Validator-Konfiguration.

Die Dateien werden nicht in Git aufgenommen. `scripts/prepare_windows_components.py` lädt sie in einen lokalen Cache, prüft jeden Hash und bereitet anschließend `runtime/java/` und `vendor/kosit/` für PyInstaller vor. ZIP-Ziele werden vor dem Entpacken gegen Pfadtraversierung geprüft.

Bei einer Aktualisierung darf nicht nur die URL geändert werden: Version, Dateiname und veröffentlichte SHA-256-Prüfsumme müssen gemeinsam überprüft und im Lockfile angepasst werden. Anschließend sind mindestens eine akzeptierte und eine abgelehnte Rechnung real mit KoSIT zu testen.

## Lokaler Build auf Windows

Voraussetzungen:

- Windows-x64-Python 3.13;
- Inno Setup 6 oder 7;
- Netzwerkzugriff beim Vorbereiten der gesperrten Komponenten.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . -r packaging\windows\requirements-build.txt
python scripts\prepare_windows_components.py
.\scripts\build_windows.ps1
.\scripts\test_windows_package.ps1
```

Ein bewusst reduzierter Entwickler-Build ohne Java/KoSIT ist mit `build_windows.ps1 -WithoutOfficialValidation` möglich. Er darf nicht als vollständiges Endbenutzerpaket veröffentlicht werden.

Der Build verwendet PyInstaller im `onedir`-Modus. Inno Setup packt dieses Verzeichnis in eine einzelne Setup-Datei. `vendor/`, `runtime/`, `.cache/` und das erzeugte Paket bleiben von Git und vom bereinigten Repository-Release ausgeschlossen.

## Build vom Intel-Mac

PyInstaller ist kein Cross-Compiler. Quellcode, Tests, Spec- und Installerdateien können auf macOS entwickelt werden; der Windows-Build läuft im vorhandenen GitHub-Actions-Job auf `windows-2022`. Jeder Pull Request erhält das unsignierte Setup als kurzlebiges Actions-Artefakt für Tests. Vor einer öffentlichen Veröffentlichung ist zusätzlich eine manuelle Prüfung auf einer sauberen Windows-11-x64-VM erforderlich.

## Authenticode-Signierung

`scripts/build_windows.ps1` signiert zuerst die anwendungseigene `E-Rechnungs-Pruefer.exe` und danach den Installer. Jede Signatur erhält einen RFC-3161-Zeitstempel und wird unmittelbar mit Windows SignTool verifiziert. Bereits signierte Drittkomponenten wie die eingebettete Java-Laufzeit werden nicht mit einer COMPESO-Signatur überschrieben.

Für lokale Windows-Builds kann `EINVOICE_SIGN_CERT_SHA1` weiterhin auf ein RSA-Code-Signing-Zertifikat im persönlichen Windows-Zertifikatsspeicher verweisen.

Der Release-Workflow verwendet dagegen AzureSignTool 7.0.1 und den nicht exportierbaren HSM-Schlüssel in Azure Key Vault. GitHub Actions meldet sich kennwortlos über OpenID Connect bei Azure an. Weder ein PFX noch ein Client-Secret wird in GitHub gespeichert. Der Windows-Job ist an die geschützte GitHub-Umgebung `release` gebunden und benötigt dort:

- Environment-Secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` und `AZURE_SUBSCRIPTION_ID`;
- Environment-Variable `AZURE_KEY_VAULT_URL`;
- Environment-Variable `AZURE_CODE_SIGNING_CERTIFICATE`.

Der manuelle Start des Release-Workflows erzeugt nur ein signiertes Actions-Artefakt für die interne Prüfung. Ein öffentlicher GitHub Release entsteht ausschließlich bei einem passenden `v*`-Tag. Der Workflow bricht ab, wenn Anmeldung, HSM-Signierung oder Signaturprüfung fehlschlägt.

## Automatischer Pakettest

`scripts/test_windows_package.ps1` prüft auf dem Windows-Runner:

1. stille Installation in einen Pfad mit Leerzeichen;
2. Start des eingefrorenen Programms;
3. Healthcheck und eingerichtete KoSIT-Komponenten;
4. token-geschützte CII-Analyse;
5. reale KoSIT-Ausführung;
6. byteidentischen XML-Export;
7. Prozessende und stille Deinstallation;
8. Entfernung der Programm- und Laufzeitdateien.

Der automatisierte Test ersetzt nicht die visuelle Prüfung von Installer, Infobereich, Standardbrowser, Defender/SmartScreen und Deinstallation auf Windows 11.

## Drittkomponenten

Die mitgelieferten Lizenz- und NOTICE-Dateien der offiziellen Archive bleiben im Bundle erhalten, soweit sie Bestandteil der Archive sind. Ergänzende Angaben stehen in `THIRD_PARTY.md`. Vor kommerzieller Verwendung sind insbesondere die aktuellen Bedingungen von Inno Setup und die Weitergabebedingungen aller gebündelten Komponenten zu prüfen.
