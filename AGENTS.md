# AGENTS.md

This file contains repository-wide instructions for Codex and other coding agents.
More specific `AGENTS.md` files may be added below a directory when a subsystem needs stricter rules.

## Project purpose

E-Rechnungs-Pruefer is a local FastAPI application for viewing and validating structured electronic invoices.
It accepts CII/UN CEFACT CrossIndustryInvoice, UBL Invoice/CreditNote, and hybrid PDF files with embedded XML.
The default UI language and user-facing findings are German.

## Repository map

- `app/source.py`: input detection and safe extraction of embedded PDF attachments.
- `app/xml_utils.py`: hardened XML parsing and low-level XML utilities.
- `app/parsers/cii.py`, `app/parsers/ubl.py`: syntax-specific mapping into the normalized invoice model.
- `app/validators/builtin.py`: transparent plausibility, calculation, and semantic checks.
- `app/validators/kosit.py`: optional KoSIT process integration and VARL report parsing.
- `app/main.py`: FastAPI endpoints and HTML report generation.
- `app/static/`, `app/templates/`: browser UI and standalone HTML report.
- `tests/`: anonymized regression tests. Add new regressions here before changing behavior.
- `scripts/`: developer bootstrap, validation, release, Git initialization, and KoSIT installation.
- `docs/`: architecture, validation scope, tax-category behavior, security, and release documentation.

## Required checks

Run the complete local gate before proposing a change:

```sh
./scripts/check.sh
```

On Windows:

```powershell
.\scripts\check.ps1
```

Equivalent individual commands:

```sh
python scripts/verify_version.py
python -m ruff check app tests scripts
python -m ruff format --check app tests scripts
python -m mypy
python -m pytest --cov=app --cov-report=term-missing
```

Use `make format` to apply Python formatting. Do not reformat HTML, CSS, or JavaScript wholesale unless the task requires it.

## Autonome Abnahme und Befundübergabe

- Organisiere Release-Abnahmen verbindlich nach [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md). Pro Lauf gibt es
  genau einen schreibenden Controller; `acceptance-plan.json` und die versiegelte Evidence sind maßgeblich,
  niemals der Chatverlauf.
- Führe alle technisch möglichen Abnahmeschritte innerhalb des freigegebenen Scopes selbst aus. Dazu gehören insbesondere Host- und Gastbefehle, Read-only-Inventuren, Harness- und Testläufe, Evidence-Prüfungen sowie ausdrücklich autorisierte VM-Schritte. Fordere den Nutzer nur für eine UAC-Bestätigung, eine erforderliche PIN- oder Passworteingabe oder eine tatsächlich menschliche visuelle Prüfung an; Zugangsdaten niemals erfragen, speichern oder umgehen.
- Verwende für jeden Abnahmekontext, dessen Fortsetzung auf UAC, PIN, Passwort oder eine echte manuelle Sichtprüfung wartet, eine Gültigkeit von 120 Minuten. Binde diese Frist ausdrücklich in Generator, Kontext, Laufzeitprüfung und Regressionstests. Rein autonome Kontexte dürfen enger begrenzt bleiben. Eine längere Frist lockert keine Bindung an VM, Snapshot, Identität, Artefakte, Hashes oder Kollisionsfreiheit.
- Handle fail-closed. Prüfe vor Produkt- oder VM-Aktionen die exakten Ziele, Hash- und Kontextbindungen, Kollisionsfreiheit und erforderlichen Receipts. Führe bei fehlenden, veralteten oder widersprüchlichen Bindungen keine Mutation aus. Führe VM-Aktionen nur in der autorisierten Reihenfolge und ausschließlich gegen die gebundene VM beziehungsweise den gebundenen Snapshot aus; verifiziere den Zustand nach jedem Schritt erneut.
- Unterbrich bei einem Abnahmebefund zunächst den betroffenen mutierenden Schritt, Wiederholungen sowie Cleanup- oder Restore-Aktionen. Bewahre beziehungsweise prüfe die vorgesehene Evidence, trenne bestätigte Fakten klar von offenen Aussagen und klassifiziere den Befund, bevor du fortfährst.
- Diagnose und behebe Harness-, Evidence-, Controller-, Umgebungs- und andere innerhalb des Abnahmelaufs sicher korrigierbare Testfehler im selben Chat. Bewahre frühere Revisionen und Evidence unverändert, verwende für Korrekturen eine getrennte, eindeutig versionierte Harness-Revision, prüfe sie unabhängig und setze anschließend die Abnahme fort.
- Stoppe die weitere Abnahme und erstelle einen direkt nutzbaren Übergabe-Prompt und/oder eine kurze Befunddokumentation für einen separaten Chat nur bei einem bestätigten Produktfehler, der innerhalb des Abnahmelaufs nicht sicher behebbar ist und einen neuen Release-Kandidaten erfordert. Nenne darin mindestens die exakten Artefakt- und Evidence-Pfade, Hash- und Versionsbindungen, den beobachteten Fehler, Reproduktionsschritte, erwartetes und tatsächliches Verhalten, den erhaltenen Systemzustand sowie ausdrücklich erlaubte und verbotene Folgeaktionen.

## Behavioral invariants

1. Uploaded invoices are not persisted by the application.
2. Original XML bytes must remain byte-for-byte exportable through `/api/xml`.
3. DTD and ENTITY declarations remain rejected. XML entity resolution, DTD loading, and network access remain disabled.
4. A PDF without an embedded structured invoice XML must not be reconstructed with OCR or presented as a valid E-invoice.
5. A technical Java, JAR, timeout, or configuration failure must never be reported as a KoSIT rejection of the invoice.
6. When a valid KoSIT VARL report exists, `<rep:accept/>` or `<rep:reject/>` is authoritative; process exit code differences are warnings.
7. Unknown XML data must remain available in the technical appendix even when the normalized model does not understand it.
8. Internal findings are transparent application heuristics. They are not a substitute for XSD/Schematron validation, tax advice, or legal review.

## Tax-category rules

- Preserve and display the raw category code.
- Display the human-readable mapping, basis amount, exemption reason, and exemption-reason code together; never hide the reason merely because a basis exists.
- Category `O` means outside the VAT scope and must not contain a VAT rate.
- Categories `Z`, `E`, `AE`, `G`, and `K` require a zero rate when the rate field is applicable.
- Semantic checks such as `G` combined with “nicht im Inland steuerbar” are warnings, not automatic tax-law determinations.
- Add an anonymized regression test for every change to tax display or validation.

## Data and fixture safety

- Never commit real invoices, names, email addresses, IBANs, tax IDs, customer numbers, or document hashes from users.
- Fixtures must be synthetic and clearly named as examples.
- Keep `vendor/`, `.env*`, reports, local uploads, generated distributions, and virtual environments out of Git.
- The release builder intentionally excludes unapproved XML files and all PDF/key material. Do not weaken this safeguard without a documented reason and tests.

## Coding conventions

- Python 3.11 is the minimum supported version.
- Prefer small, typed functions and explicit dictionaries over hidden mutation.
- User-visible messages are German; source comments and developer documentation may be English or German.
- Keep parser changes syntax-specific and shared presentation logic in `app/parsers/common.py` or `app/code_lists.py`.
- Avoid adding a runtime dependency when the standard library is sufficient.
- Do not call external services during invoice analysis. The KoSIT installer is an explicit, user-triggered setup step.
- Treat XML, filenames, subprocess output, and uploaded metadata as untrusted input.

## Versioning and releases

The version must match in `VERSION`, `pyproject.toml`, `app/__init__.py`, and the KoSIT installer user agent.
Update `CHANGELOG.md` for user-visible changes. Build release artefacts with:

```sh
python scripts/build_release.py
```

The resulting repository ZIP must contain no `.git`, `.venv`, `vendor`, `.env`, real invoices, or generated reports.
