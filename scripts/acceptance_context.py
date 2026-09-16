#!/usr/bin/env python3
"""Bind acceptance actions and optionally wrap an explicitly supplied CI test command."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

SCHEMA = "e-rechnungs-pruefer-acceptance-context/v1"
PREFLIGHT_SCHEMA = "e-rechnungs-pruefer-acceptance-preflight/v1"
TERMINAL = {"PASS", "FAIL_PRODUCT", "FAIL_HARNESS", "FAIL_ENVIRONMENT", "INCONCLUSIVE", "ABORTED"}
WAIT_REASONS = {"uac", "pin", "password", "visual"}
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+ -]{0,199}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


class ContextError(ValueError):
    """A binding cannot safely authorize the requested action."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return sha256(canonical(value)).hexdigest()


def timestamp(value: datetime) -> str:
    if value.tzinfo != UTC:
        raise ContextError("Zeitstempel muss UTC verwenden.")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContextError("Ungültiger UTC-Zeitstempel.")
    try:
        return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ContextError("Ungültiger UTC-Zeitstempel.") from exc


def _now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    timestamp(result)
    return result


def _label(value: Any) -> None:
    if not isinstance(value, str) or not LABEL.fullmatch(value) or value in {".", ".."}:
        raise ContextError("Ungültige Identität oder Bezeichnung.")


def _keys(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContextError("Unbekanntes oder unvollständiges Schema.")


def _safe_path(path: Path) -> Path:
    if ".." in path.parts:
        raise ContextError("Übergeordnete Pfadsegmente sind nicht erlaubt.")
    result = Path(os.path.abspath(path))
    for candidate in (*reversed(result.parents), result):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ContextError(f"Link oder Reparse-Point ist nicht erlaubt: {candidate}")
    return result


def _read(path: Path) -> bytes:
    path = _safe_path(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ContextError(f"Keine einzelne reguläre Datei: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        result = stream.read()
        after = os.fstat(stream.fileno())
        final = path.stat()

    def identity(metadata: os.stat_result, *, cross_api: bool = False) -> tuple[int, ...]:
        # CPython adds extension-derived execute bits only to Windows path-stat.
        # Keep full modes for before/after checks within each individual API.
        mode = metadata.st_mode & ~0o111 if cross_api and sys.platform == "win32" else metadata.st_mode
        return (metadata.st_dev, metadata.st_ino, mode, metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns)

    if (
        identity(before) != identity(final)
        or identity(opened) != identity(after)
        or identity(before, cross_api=True) != identity(opened, cross_api=True)
        or len(result) != before.st_size
    ):
        raise ContextError(f"Datei während der Prüfung verändert: {path}")
    return result


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContextError("Doppelter JSON-Schlüssel.")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(_read(path), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContextError("Ungültiges JSON.") from exc
    if not isinstance(result, dict):
        raise ContextError("JSON-Objekt erforderlich.")
    return result


def _file(path: Path) -> dict[str, Any]:
    path = _safe_path(path)
    content = _read(path)
    return {"path": str(path), "name": path.name, "size": len(content), "sha256": sha256(content).hexdigest()}


def validate_binding(value: dict[str, Any]) -> None:
    common = {"kind", "commit", "version"}
    if value.get("kind") == "ci":
        _keys(
            value,
            common | {"workflow_run", "workflow_attempt", "job", "runner", "runner_os", "runner_arch", "repository"},
        )
        for name in ("workflow_run", "workflow_attempt"):
            if not re.fullmatch(r"[1-9][0-9]*", str(value[name])):
                raise ContextError("Ungültige Workflow-Bindung.")
    elif value.get("kind") == "vm":
        _keys(value, common | {"vm_uuid", "snapshot_uuid", "identity", "os_version"})
        for name in ("vm_uuid", "snapshot_uuid"):
            try:
                parsed = UUID(value[name])
            except (ValueError, TypeError, AttributeError) as exc:
                raise ContextError("Ungültige VM-/Snapshot-Bindung.") from exc
            if str(parsed) != value[name] or parsed.int == 0:
                raise ContextError("Ungültige VM-/Snapshot-Bindung.")
    else:
        raise ContextError("Unbekannte Zielbindung.")
    for text in value.values():
        _label(text)
    if not re.fullmatch(r"[0-9a-f]{40}", value["commit"]):
        raise ContextError("Vollständige Commit-Bindung erforderlich.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"]):
        raise ContextError("Ungültige Versionsbindung.")


def checkout_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def ci_binding(version: str) -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ContextError("GitHub-Actions-Runner erforderlich.")
    commit = checkout_commit()
    if commit != os.environ.get("GITHUB_SHA"):
        raise ContextError("Checkout stimmt nicht mit GITHUB_SHA überein.")
    result = {"kind": "ci", "commit": commit, "version": version}
    names = {
        "workflow_run": "GITHUB_RUN_ID",
        "workflow_attempt": "GITHUB_RUN_ATTEMPT",
        "job": "GITHUB_JOB",
        "runner": "RUNNER_NAME",
        "runner_os": "RUNNER_OS",
        "runner_arch": "RUNNER_ARCH",
        "repository": "GITHUB_REPOSITORY",
    }
    result.update({key: os.environ.get(name, "") for key, name in names.items()})
    validate_binding(result)
    return result


def _exclusive(path: Path, content: bytes) -> None:
    path = _safe_path(path)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), "wb"
    ) as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _writer(root: Path) -> Iterator[Path]:
    root = _safe_path(root)
    lock = root / ".writer-lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ContextError("Ein Controller schreibt bereits oder sein Lauf wurde unterbrochen.") from exc
    try:
        yield root
    finally:
        lock.rmdir()


def _persist(root: Path, state: dict[str, Any], action: str) -> None:
    previous = state.pop("event_sha256", "0" * 64)
    state["revision"] += 1
    event = {"schema": SCHEMA, "sequence": state["revision"], "previous": previous, "action": action, "state": state}
    event_hash = digest(event)
    event["sha256"] = event_hash
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(root / "events.ndjson", flags), "ab") as stream:
        stream.write(canonical(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    state["event_sha256"] = event_hash
    descriptor, temporary = tempfile.mkstemp(prefix=".plan-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(state) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "acceptance-plan.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _validate_context(context_id: str, context: dict[str, Any], scope: list[str]) -> None:
    fields = {
        "id",
        "action",
        "status",
        "created_at_utc",
        "expires_at_utc",
        "autonomous_minutes",
        "user_wait",
        "require_signatures",
        "artifacts",
        "command",
    }
    if "consumed_at_utc" in context:
        fields |= {"consumed_at_utc", "preflight"}
    if context.get("status") in TERMINAL:
        fields |= {"receipt", "receipt_sha256"}
    _keys(context, fields)
    if (
        context["id"] != context_id
        or str(UUID(context_id)) != context_id
        or context["action"] not in scope
        or context["status"] not in {"READY", "RUNNING"} | TERMINAL
        or type(context["require_signatures"]) is not bool
    ):
        raise ContextError("Ungültiges Kontextschema.")
    wait = context["user_wait"]
    if wait is not None and (wait not in WAIT_REASONS or context["autonomous_minutes"] is not None):
        raise ContextError("Ungültiger Benutzerkontext.")
    minutes = 120 if wait else context["autonomous_minutes"]
    created, expires = parse_timestamp(context["created_at_utc"]), parse_timestamp(context["expires_at_utc"])
    if type(minutes) is not int or not 1 <= minutes <= 120 or expires - created != timedelta(minutes=minutes):
        raise ContextError("Ungültige Kontextgültigkeit.")
    if context["status"] in {"RUNNING", "PASS"} and "consumed_at_utc" not in context:
        raise ContextError("Konsumierter Claim fehlt.")
    if "consumed_at_utc" in context and not created <= parse_timestamp(context["consumed_at_utc"]) < expires:
        raise ContextError("Claim wurde außerhalb seiner Gültigkeit konsumiert.")
    if not isinstance(context["artifacts"], list) or not context["artifacts"]:
        raise ContextError("Artefaktbindung fehlt.")
    command = context["command"]
    if command is not None and (
        not isinstance(command, list) or not command or any(not isinstance(arg, str) or "\0" in arg for arg in command)
    ):
        raise ContextError("Ungültige Befehlsbindung.")
    for artifact in context["artifacts"]:
        _keys(artifact, {"path", "name", "size", "sha256"})
        if (
            not isinstance(artifact["path"], str)
            or not Path(artifact["path"]).is_absolute()
            or Path(artifact["path"]).name != artifact["name"]
            or type(artifact["size"]) is not int
            or artifact["size"] < 0
            or not isinstance(artifact["sha256"], str)
            or not HASH.fullmatch(artifact["sha256"])
        ):
            raise ContextError("Ungültiges Artefaktschema.")


def _verify_receipts(root: Path, state: dict[str, Any]) -> None:
    expected_files: set[str] = set()
    for context in state["contexts"].values():
        if context["status"] not in TERMINAL:
            continue
        relative = f"receipts/{context['id']}.json"
        if context["receipt"] != relative:
            raise ContextError("Ungültiger Abschlusspfad.")
        content = _read(root / relative)
        checksum = sha256(content).hexdigest()
        if checksum != context["receipt_sha256"]:
            raise ContextError("Abschlussreceipt wurde verändert.")
        detached = f"receipts/{context['id']}.sha256"
        if _read(root / detached) != f"{checksum}  {context['id']}.json\n".encode():
            raise ContextError("Abschlussprüfsumme wurde verändert.")
        report = read_json(root / relative)
        if (
            report["status"] != context["status"]
            or report["run_id"] != state["run_id"]
            or report["binding"] != state["binding"]
            or report["controller"] != state["controller"]
        ):
            raise ContextError("Abschlussbindung stimmt nicht überein.")
        expected_files.update({relative, detached})
        for index, record in enumerate(report["evidence"]):
            path = f"receipts/{context['id']}-evidence-{index}.bin"
            actual = _file(root / path)
            if (
                record["path"] != path
                or record["name"] != actual["name"]
                or record["size"] != actual["size"]
                or record["sha256"] != actual["sha256"]
            ):
                raise ContextError("Abschlussbeleg wurde verändert.")
            expected_files.add(path)
    actual_files = {file.relative_to(root).as_posix() for file in (root / "receipts").iterdir()}
    if actual_files != expected_files:
        raise ContextError("Ungebundene oder fehlende Abschlussdateien; Lauf ist inkonsistent.")


def verify(root: Path) -> dict[str, Any]:
    root = _safe_path(root)
    state = read_json(root / "acceptance-plan.json")
    _keys(
        state,
        {
            "schema",
            "run_id",
            "controller",
            "binding",
            "scope",
            "created_at_utc",
            "updated_at_utc",
            "revision",
            "blocked",
            "contexts",
            "event_sha256",
        },
    )
    if state["schema"] != SCHEMA or type(state["blocked"]) is not bool or type(state["revision"]) is not int:
        raise ContextError("Ungültiges Planschema.")
    validate_binding(state["binding"])
    _label(state["controller"])
    parse_timestamp(state["created_at_utc"])
    parse_timestamp(state["updated_at_utc"])
    if not isinstance(state["scope"], list) or not state["scope"] or len(set(state["scope"])) != len(state["scope"]):
        raise ContextError("Ungültiger Scope.")
    for action in state["scope"]:
        _label(action)
    if not isinstance(state["contexts"], dict):
        raise ContextError("Ungültiges Kontextschema.")
    for context_id, context in state["contexts"].items():
        _validate_context(context_id, context, state["scope"])
    if not state["blocked"] and any(context["status"] in TERMINAL - {"PASS"} for context in state["contexts"].values()):
        raise ContextError("Terminaler Befund ohne erforderliche Sperre des Laufs.")
    previous = "0" * 64
    last = None
    try:
        for sequence, line in enumerate(_read(root / "events.ndjson").splitlines(), 1):
            event = json.loads(line, object_pairs_hook=_pairs)
            _keys(event, {"schema", "sequence", "previous", "action", "state", "sha256"})
            expected = event.pop("sha256")
            if (
                event["schema"] != SCHEMA
                or event["sequence"] != sequence
                or event["previous"] != previous
                or digest(event) != expected
            ):
                raise ContextError("Ereigniskette ist widersprüchlich.")
            previous, last = expected, event["state"]
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ContextError("Ereignisprotokoll ist unvollständig.") from exc
    comparison = dict(state)
    comparison.pop("event_sha256")
    if last != comparison or previous != state["event_sha256"]:
        raise ContextError("Plan und Ereignisprotokoll widersprechen sich.")
    _verify_receipts(root, state)
    return state


def initialize(
    root: Path, controller: str, binding: dict[str, Any], scope: list[str], *, now: datetime | None = None
) -> dict[str, Any]:
    root = _safe_path(root)
    validate_binding(binding)
    _label(controller)
    if not scope or len(set(scope)) != len(scope):
        raise ContextError("Ein eindeutiger Aktionsscope ist erforderlich.")
    for action in scope:
        _label(action)
    moment = timestamp(_now(now))
    state = {
        "schema": SCHEMA,
        "run_id": str(uuid4()),
        "controller": controller,
        "binding": binding,
        "scope": scope,
        "created_at_utc": moment,
        "updated_at_utc": moment,
        "revision": 0,
        "blocked": False,
        "contexts": {},
    }
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ContextError("Evidence-Ziel existiert bereits; keine Wiederverwendung.") from exc
    (root / "receipts").mkdir(mode=0o700)
    _exclusive(root / "events.ndjson", b"")
    with _writer(root):
        _persist(root, state, "initialize")
    return state


def _owned(root: Path, controller: str) -> dict[str, Any]:
    state = verify(root)
    if state["controller"] != controller:
        raise ContextError("Controller stimmt nicht überein.")
    return state


def issue(
    root: Path,
    controller: str,
    action: str,
    artifacts: list[Path],
    *,
    autonomous_minutes: int | None = None,
    user_wait: str | None = None,
    require_signatures: bool = False,
    command: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _now(now)
    if user_wait is not None:
        if user_wait not in WAIT_REASONS or autonomous_minutes is not None:
            raise ContextError("Ungültige Gültigkeit für Benutzerkontext.")
        minutes = 120
    elif type(autonomous_minutes) is int and 1 <= autonomous_minutes <= 120:
        minutes = autonomous_minutes
    else:
        raise ContextError("Autonome Gültigkeit muss ausdrücklich zwischen 1 und 120 Minuten liegen.")
    if not artifacts:
        raise ContextError("Mindestens ein Artefakt erforderlich.")
    if command is not None and (not command or any(not isinstance(arg, str) or "\0" in arg for arg in command)):
        raise ContextError("Ungültige Befehlsbindung.")
    files = [_file(path) for path in artifacts]
    if len({f["path"] for f in files}) != len(files):
        raise ContextError("Doppelte Artefaktpfade.")
    with _writer(root) as root:
        state = _owned(root, controller)
        if moment < parse_timestamp(state["updated_at_utc"]):
            raise ContextError("Kontextzeit liegt vor der letzten Zustandsänderung.")
        if state["blocked"]:
            raise ContextError("Lauf ist nach einem Befund gesperrt.")
        if action not in state["scope"]:
            raise ContextError("Aktion liegt außerhalb des Scope.")
        if any(c["status"] == "RUNNING" for c in state["contexts"].values()):
            raise ContextError("Andere Aktion läuft; parallele Mutation gesperrt.")
        context = {
            "id": str(uuid4()),
            "action": action,
            "status": "READY",
            "created_at_utc": timestamp(moment),
            "expires_at_utc": timestamp(moment + timedelta(minutes=minutes)),
            "autonomous_minutes": autonomous_minutes,
            "user_wait": user_wait,
            "require_signatures": require_signatures,
            "artifacts": files,
            "command": command,
        }
        state["contexts"][context["id"]] = context
        state["updated_at_utc"] = timestamp(moment)
        _persist(root, state, "issue")
        return context


def _preflight(
    path: Path | None, context: dict[str, Any], binding: dict[str, Any], now: datetime
) -> dict[str, Any] | None:
    if path is None:
        if binding["kind"] == "vm" or context["require_signatures"]:
            raise ContextError("Frische gebundene Vorprüfung erforderlich.")
        return None
    report = read_json(path)
    _keys(
        report,
        {
            "schema",
            "binding_sha256",
            "context_id",
            "observed_at_utc",
            "collision_free",
            "state_verified",
            "signatures_verified",
        },
    )
    age = now - parse_timestamp(report["observed_at_utc"])
    if (
        report["schema"] != PREFLIGHT_SCHEMA
        or report["binding_sha256"] != digest(binding)
        or report["context_id"] != context["id"]
        or not timedelta(0) <= age <= timedelta(minutes=5)
        or report["collision_free"] is not True
        or report["state_verified"] is not True
        or (context["require_signatures"] and report["signatures_verified"] is not True)
    ):
        raise ContextError("Vorprüfung fehlt, ist widersprüchlich oder veraltet.")
    return _file(path)


def guard(
    root: Path,
    controller: str,
    context_id: str,
    binding: dict[str, Any],
    *,
    preflight: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = _now(now)
    with _writer(root) as root:
        state = _owned(root, controller)
        if moment < parse_timestamp(state["updated_at_utc"]):
            raise ContextError("Guardzeit liegt vor der letzten Zustandsänderung.")
        if state["blocked"]:
            raise ContextError("Lauf ist gesperrt.")
        if state["binding"] != binding:
            raise ContextError("Aktuelle Zielbindung stimmt nicht mit der Bindung des Laufs überein.")
        context = state["contexts"].get(context_id)
        if context is None or context["status"] != "READY":
            raise ContextError("Kontext fehlt oder sein einmaliger Claim ist bereits verbraucht.")
        if any(c["status"] == "RUNNING" for c in state["contexts"].values()):
            raise ContextError("Andere Aktion läuft; parallele Mutation gesperrt.")
        created = parse_timestamp(context["created_at_utc"])
        expires = parse_timestamp(context["expires_at_utc"])
        minutes = 120 if context["user_wait"] in WAIT_REASONS else context["autonomous_minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 120 or expires - created != timedelta(minutes=minutes):
            raise ContextError("Ungültige Kontextgültigkeit.")
        if moment < created or moment >= expires:
            raise ContextError("Kontext ist abgelaufen oder die Uhr widersprüchlich.")
        for artifact in context["artifacts"]:
            if _file(Path(artifact["path"])) != artifact:
                raise ContextError("Artefaktname, Größe, Pfad oder SHA-256 stimmt nicht überein.")
        context["preflight"] = _preflight(preflight, context, binding, moment)
        context["status"] = "RUNNING"
        context["consumed_at_utc"] = timestamp(moment)
        state["updated_at_utc"] = timestamp(moment)
        _persist(root, state, "guard")
        return context


def complete(
    root: Path, controller: str, context_id: str, status: str, evidence: list[Path], *, now: datetime | None = None
) -> dict[str, Any]:
    if status not in TERMINAL or not evidence:
        raise ContextError("Terminaler Status und mindestens ein Rohbeleg erforderlich.")
    moment = _now(now)
    with _writer(root) as root:
        state = _owned(root, controller)
        context = state["contexts"].get(context_id)
        permitted = {"RUNNING", "READY"} if status in {"ABORTED", "INCONCLUSIVE"} else {"RUNNING"}
        if context is None or context["status"] not in permitted:
            raise ContextError("Kontext wurde nicht konsumiert oder ist bereits abgeschlossen.")
        if moment < parse_timestamp(state["updated_at_utc"]):
            raise ContextError("Abschlusszeit liegt vor der letzten Zustandsänderung.")
        receipts = root / "receipts"
        records = []
        contents = [_read(source) for source in evidence]
        if any(not content for content in contents):
            raise ContextError("Leerer Rohbeleg ist kein Abschlussnachweis.")
        for index, (source, content) in enumerate(zip(evidence, contents, strict=True)):
            destination = receipts / f"{context_id}-evidence-{index}.bin"
            _exclusive(destination, content)
            record = _file(destination)
            record["path"] = destination.relative_to(root).as_posix()
            record["source_name"] = source.name
            records.append(record)
        result = {
            "schema": SCHEMA,
            "run_id": state["run_id"],
            "binding": state["binding"],
            "controller": controller,
            "context": dict(context),
            "status": status,
            "completed_at_utc": timestamp(moment),
            "evidence": records,
        }
        receipt = receipts / f"{context_id}.json"
        content = canonical(result) + b"\n"
        _exclusive(receipt, content)
        _exclusive(receipts / f"{context_id}.sha256", f"{sha256(content).hexdigest()}  {receipt.name}\n".encode())
        context["status"] = status
        context["receipt"] = receipt.relative_to(root).as_posix()
        context["receipt_sha256"] = sha256(content).hexdigest()
        state["blocked"] = state["blocked"] or status != "PASS"
        state["updated_at_utc"] = timestamp(moment)
        _persist(root, state, "complete")
        return result


def run_ci(
    root: Path,
    controller: str,
    action: str,
    artifacts: list[Path],
    scripts: list[Path],
    evidence: Path,
    command: list[str],
) -> tuple[dict[str, Any], int]:
    """Run a reviewed argv only after consuming its bound CI claim; never infer product failure."""
    state = _owned(root, controller)
    if state["binding"]["kind"] != "ci":
        raise ContextError("run-ci ist ausschließlich für gebundene CI-Läufe erlaubt.")
    if not command or not scripts:
        raise ContextError("Ein expliziter Befehl und mindestens ein gebundenes Skript sind erforderlich.")
    argument_paths = set()
    for argument in command[1:]:
        try:
            argument_paths.add(_safe_path(Path(argument)))
        except (ContextError, OSError, ValueError):
            # Ordinary flags/values are not paths; the direct script argument must match.
            continue
    for script in scripts:
        if _safe_path(script) not in argument_paths:
            raise ContextError("Gebundenes Skript fehlt als separates Befehlsargument.")
    evidence = _safe_path(evidence)
    if evidence.exists():
        raise ContextError("Rohlog existiert bereits und wird nicht überschrieben.")
    files = list(dict.fromkeys([_safe_path(path) for path in [*artifacts, *scripts]]))
    context = issue(root, controller, action, files, autonomous_minutes=30, command=command)
    guard(root, controller, context["id"], ci_binding(state["binding"]["version"]))
    header = {"event": "harness-command-start", "context_id": context["id"], "command": command}
    _exclusive(evidence, canonical(header) + b"\n")
    with evidence.open("ab", buffering=0) as stream:
        try:
            process = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=False)
            code = process.returncode
        except OSError as exc:
            stream.write(canonical({"event": "harness-start-error", "error": str(exc)}) + b"\n")
            code = 1
        stream.write(b"\n" + canonical({"event": "harness-command-end", "exit_code": code}) + b"\n")
        os.fsync(stream.fileno())
    receipt = complete(root, controller, context["id"], "PASS" if code == 0 else "INCONCLUSIVE", [evidence])
    return receipt, code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init-ci", "init", "issue", "guard", "complete", "verify", "run-ci"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        if name != "verify":
            command.add_argument("--controller", required=True)
        if name in {"init-ci", "init"}:
            command.add_argument("--scope", action="append", required=True)
            command.add_argument("--version", required=True) if name == "init-ci" else command.add_argument(
                "--binding", type=Path, required=True
            )
        elif name == "run-ci":
            command.add_argument("--action", required=True)
            command.add_argument("--artifact", type=Path, action="append", required=True)
            command.add_argument("--script", type=Path, action="append", required=True)
            command.add_argument("--evidence", type=Path, required=True)
            command.add_argument("argv", nargs=argparse.REMAINDER)
        elif name == "issue":
            command.add_argument("--action", required=True)
            command.add_argument("--artifact", type=Path, action="append", required=True)
            expiry = command.add_mutually_exclusive_group(required=True)
            expiry.add_argument("--autonomous-minutes", type=int)
            expiry.add_argument("--user-wait", choices=sorted(WAIT_REASONS))
            command.add_argument("--require-signatures", action="store_true")
        elif name in {"guard", "complete"}:
            command.add_argument("--context", required=True)
            if name == "guard":
                target = command.add_mutually_exclusive_group(required=True)
                target.add_argument("--ci", action="store_true")
                target.add_argument("--binding", type=Path)
                command.add_argument("--preflight", type=Path)
            else:
                command.add_argument("--status", choices=sorted(TERMINAL), required=True)
                command.add_argument("--evidence", type=Path, action="append", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    exit_code = 0
    try:
        if args.command == "verify":
            result = verify(args.root)
        elif args.command in {"init-ci", "init"}:
            binding = ci_binding(args.version) if args.command == "init-ci" else read_json(args.binding)
            result = initialize(args.root, args.controller, binding, args.scope)
        elif args.command == "issue":
            result = issue(
                args.root,
                args.controller,
                args.action,
                args.artifact,
                autonomous_minutes=args.autonomous_minutes,
                user_wait=args.user_wait,
                require_signatures=args.require_signatures,
            )
        elif args.command == "run-ci":
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            result, exit_code = run_ci(
                args.root, args.controller, args.action, args.artifact, args.script, args.evidence, command
            )
        elif args.command == "guard":
            binding = ci_binding(verify(args.root)["binding"]["version"]) if args.ci else read_json(args.binding)
            result = guard(args.root, args.controller, args.context, binding, preflight=args.preflight)
        else:
            result = complete(args.root, args.controller, args.context, args.status, args.evidence)
    except (ContextError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Abnahmebindung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(canonical(result).decode())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
