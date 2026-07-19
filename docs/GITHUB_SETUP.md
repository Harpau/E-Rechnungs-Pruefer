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
git commit -m "Initial import of E-Rechnungs-Pruefer 1.1.0"
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

- `ci.yml`: Lint, Format, Typen, Coverage, Python-Matrix, Windows-Smoke-Test und Docker-Build
- `codeql.yml`: statische Sicherheitsanalyse für Python und JavaScript
- `dependency-audit.yml`: regelmäßige Prüfung der Python-Abhängigkeiten
- `release.yml`: Tagprüfung, Tests, Build, Prüfsummen und GitHub Release

Für Releases benötigt der Workflow Schreibrechte auf `contents`. Diese werden nur im Release-Job angefordert.

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

Ein signierter oder annotierter Tag löst den Release-Workflow aus:

```sh
git tag -a v1.1.0 -m "E-Rechnungs-Pruefer 1.1.0"
git push origin v1.1.0
```

Vorher müssen `VERSION`, `pyproject.toml`, `app/__init__.py`, KoSIT-Installer und Changelog synchron sein. Details stehen in `docs/RELEASE.md`.
