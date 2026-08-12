# GitHub-Einrichtung

## Repository initialisieren

### Automatisiert

Linux/macOS:

```sh
./scripts/init_git.sh https://github.com/OWNER/REPOSITORY.git
```

Windows PowerShell:

```powershell
.\scripts\init_git.ps1 -RemoteUrl https://github.com/OWNER/REPOSITORY.git
```

Die Skripte initialisieren `main`, prüfen den Index und erstellen den ersten Commit, sofern `user.name` und `user.email` konfiguriert sind. Sie führen aus Sicherheitsgründen keinen automatischen Push durch.

### Manuell

```sh
git init -b main
git add .
git diff --cached --check
git commit -m "Initial import of E-Rechnungs-Pruefer"
git remote add origin https://github.com/OWNER/REPOSITORY.git
git push -u origin main
```

Vor `git add` immer kontrollieren, dass keine Rechnung, `.env.kosit` oder `vendor/` im Projektordner liegt.

## GitHub CLI

Nach Anmeldung mit `gh auth login` kann ein neues privates Repository angelegt werden:

```sh
gh repo create REPOSITORY --private --source=. --remote=origin --push
```

Die Sichtbarkeit später nur nach einer Prüfung auf Datenschutz, Lizenzen und echte Rechnungsdaten ändern.

## Actions

Enthaltene Workflows:

- `ci.yml`: Lint, Format, Typen, Coverage, Python-Matrix, beide Windows-x64-Installer samt Installationstests und Docker-Build
- `codeql.yml`: statische Sicherheitsanalyse für Python und JavaScript
- `dependency-audit.yml`: regelmäßige Prüfung der Python-Abhängigkeiten
- `release.yml`: Tagprüfung, Quellartefakte, beide signierten Windows-x64-Installer, Windows-Binaries-ZIP,
  Prüfsummen und einen bewusst unveröffentlicht bleibenden GitHub-Release-Draft

Für Releases benötigt der Workflow Schreibrechte auf `contents`. Diese werden nur im Release-Job angefordert.

### Geschützte Signing-Umgebung

Für die Azure-Key-Vault-Signierung muss unter **Settings → Environments** eine Umgebung namens `release` eingerichtet werden:

- mindestens einen erforderlichen Reviewer festlegen und Selbstfreigabe nur dann erlauben, wenn kein zweiter berechtigter Reviewer vorhanden ist;
- Deployment nur für den Branch `main` und Tags nach dem Muster `v*` zulassen;
- Environment-Secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` und `AZURE_SUBSCRIPTION_ID` anlegen;
- Environment-Variablen `AZURE_KEY_VAULT_URL` und `AZURE_CODE_SIGNING_CERTIFICATE` anlegen.

Die Entra-Anwendung benötigt eine federierte GitHub-Identität für genau diese Umgebung. Bei Repositorys mit unveränderlichen OIDC-Subjects müssen Owner- und Repository-ID enthalten sein. Der Key-Vault-Service-Principal erhält am Vault die Rollen `Key Vault Reader` und `Key Vault Crypto User`.

Die Windows-Installer werden nur mit gültiger Authenticode-Signatur veröffentlicht. Der Workflow speichert weder
PFX-Datei noch Client-Secret und bricht bei fehlender oder ungültiger Signatur ab. Ein manueller Workflow-Start
auf `main` erzeugt ein signiertes, für 14 Tage aufbewahrtes Vorab-Artefakt sowie ein getrenntes, nur einen Tag
verfügbares internes Recovery-Testartefakt, veröffentlicht aber keinen GitHub Release. Das interne Artefakt wird
von Tag-Läufen und dem Publish-Job ausgeschlossen und darf wegen seines erhöhten Testkontexts ausschließlich auf
einer isolierten Wegwerf-VM verwendet werden. In einem öffentlichen Repository sind diese Actions-Artefakte
nicht vertraulich: Angemeldete GitHub-Nutzer mit Repository-Lesezugriff können sie herunterladen.

## Empfohlener Branch-Schutz

Für `main`:

- Pull Request vor Merge verlangen;
- mindestens eine Review verlangen;
- erfolgreiche Checks `quality`, `tests`, `windows-smoke` und `docker` verlangen;
- Branch aktuell halten;
- direkte Force-Pushes und Branch-Löschung verbieten;
- CodeQL-Ergebnisse aktivieren, sofern im Kontomodell verfügbar.

## Repository-Einstellungen

- Private Vulnerability Reporting aktivieren.
- Secret Scanning und Push Protection aktivieren, sofern verfügbar.
- Dependabot Alerts und Security Updates aktivieren.
- Issues und Discussions nur aktivieren, wenn sie tatsächlich betreut werden.
- keine echten Rechnungen als Issue-Anhang zulassen; auf `SUPPORT.md` verweisen.

## Releases

Ein annotierter Tag löst den Release-Workflow aus:

```sh
git tag -a vX.Y.Z -m "E-Rechnungs-Pruefer X.Y.Z"
git push origin vX.Y.Z
```

Vor dem ersten Tag kann der Workflow unter **Actions → Release → Run workflow** auf `main` manuell gestartet
werden. Nach Freigabe der Umgebung `release` werden beide signierten Installer, das Windows-Binaries-ZIP und die
Prüfsummen als kurzlebiges Actions-Artefakt bereitgestellt, ohne einen öffentlichen Release anzulegen. Das
getrennte interne Recovery-Testartefakt bleibt ausschließlich für die isolierte Abnahme bestimmt.

Ein Tag-Lauf legt nach erfolgreichen Prüfungen nur einen Draft an und lädt die taggenau erzeugten Artefakte
hoch. Der Workflow veröffentlicht den Draft nicht. Vor der manuellen Veröffentlichung müssen genau diese
Draft-Dateien mindestens auf Windows 10 fokussiert geprüft und mit Tag, Commit, Workflowlauf, Prüfsummen und
Windows-/Edge-Stand protokolliert werden. Ein zuvor getestetes Preview-Artefakt ist wegen möglicher Build- und
Signaturzeitstempel kein byteidentischer Ersatz.

Partielle Reruns beziehen die erfolgreichen Vorjob-Artefakte über ihre unveränderlichen IDs. Ein bereits
angelegter Draft wird nur fortgesetzt, wenn vorhandene Assets byteidentisch sind; fremde oder abweichende
Dateien und ein bereits veröffentlichtes Release blockieren den Workflow.

Vorher müssen `VERSION`, `pyproject.toml`, `app/__init__.py`, KoSIT-Installer und Changelog synchron sein. Details stehen in `docs/RELEASE.md`.
