#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."

REMOTE_URL="${1:-}"
if [ -d .git ]; then
  echo "Dieses Verzeichnis ist bereits ein Git-Repository."
  exit 1
fi

git init -b main
git add .
git diff --cached --check

if [ -z "$(git config user.name || true)" ] || [ -z "$(git config user.email || true)" ]; then
  cat <<'MESSAGE'
Git wurde initialisiert und alle Dateien wurden vorgemerkt.
Vor dem ersten Commit bitte die Identität konfigurieren:
  git config user.name "Ihr Name"
  git config user.email "ihre-adresse@example.com"
  git commit -m "Initial import of E-Rechnungs-Pruefer 1.1.0"
MESSAGE
else
  git commit -m "Initial import of E-Rechnungs-Pruefer 1.1.0"
fi

if [ -n "$REMOTE_URL" ]; then
  git remote add origin "$REMOTE_URL"
  echo "Remote 'origin' wurde gesetzt. Push nach Prüfung mit: git push -u origin main"
else
  echo "Remote später setzen mit: git remote add origin <GitHub-URL>"
fi
