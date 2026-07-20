from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lxml import etree

from ..settings import Settings
from ..xml_utils import clean_text, local_name, namespace_uri

TECHNICAL_START_PATTERNS = (
    "no main manifest attribute",
    "kein hauptmanifestattribut",
    "unable to access jarfile",
    "invalid or corrupt jarfile",
    "could not find or load main class",
    "could not create the java virtual machine",
    "error opening zip file",
    "a jni error has occurred",
)

_REPORT_END_RE = re.compile(rb"</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?report\s*>", re.IGNORECASE)
_FORMAT_ERROR_PREFIX = b"[Format error!] <"
_FORMAT_ERROR_SUFFIX = b"> with params <"
WINDOWS_SUBPROCESS_CREATION_FLAGS = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0


@dataclass(slots=True)
class KositValidator:
    settings: Settings

    @staticmethod
    def _jar_main_class(jar_path: Path) -> str | None:
        try:
            with zipfile.ZipFile(jar_path) as archive:
                raw = archive.read("META-INF/MANIFEST.MF")
        except (OSError, KeyError, zipfile.BadZipFile):
            return None

        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        unfolded: list[str] = []
        for line in text.split("\n"):
            if line.startswith(" ") and unfolded:
                unfolded[-1] += line[1:]
            else:
                unfolded.append(line)
        for line in unfolded:
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "main-class":
                return value.strip() or None
        return None

    def configuration_state(self) -> dict[str, Any]:
        jar = self.settings.kosit_validator_jar
        scenarios = self.settings.kosit_scenarios
        problems: list[str] = []
        main_class: str | None = None
        if not self.settings.kosit_enabled:
            problems.append("KoSIT-Anbindung ist deaktiviert.")
        if jar is None:
            problems.append("KOSIT_VALIDATOR_JAR ist nicht gesetzt.")
        elif not jar.is_file():
            problems.append(f"Validator-JAR wurde nicht gefunden: {jar}")
        else:
            main_class = self._jar_main_class(jar)
            if not main_class:
                problems.append(
                    "Validator-JAR ist nicht mit 'java -jar' ausführbar, weil im Manifest die Main-Class fehlt. "
                    "Benötigt wird das offizielle '*-standalone.jar', nicht validator-<Version>.jar."
                )
        if not scenarios:
            problems.append("KOSIT_SCENARIOS ist nicht gesetzt.")
        else:
            for scenario in scenarios:
                if not scenario.is_file():
                    problems.append(f"Szenariokonfiguration wurde nicht gefunden: {scenario}")
        if shutil.which(self.settings.kosit_java_bin) is None:
            problems.append(f"Java wurde nicht gefunden: {self.settings.kosit_java_bin}")
        for repository in self.settings.kosit_repositories:
            if not repository.exists():
                problems.append(f"KoSIT-Ressourcenverzeichnis wurde nicht gefunden: {repository}")
        return {
            "configured": not problems,
            "problems": problems,
            "jar": str(jar) if jar else None,
            "jar_main_class": main_class,
            "scenarios": [str(path) for path in scenarios],
            "repositories": [str(path) for path in self.settings.kosit_repositories],
        }

    @staticmethod
    def _parse_xml_root(payload: bytes | None) -> etree._Element | None:
        if not payload:
            return None
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            recover=False,
            huge_tree=False,
        )
        try:
            return etree.fromstring(payload, parser=parser)
        except (etree.XMLSyntaxError, ValueError):
            return None

    @classmethod
    def _extract_xml_payload(cls, output: bytes) -> bytes | None:
        """Extract one complete KoSIT XML report from mixed console output."""

        if not output:
            return None

        starts = [pos for marker in (b"<?xml", b"<rep:report", b"<report") if (pos := output.find(marker)) >= 0]
        if not starts:
            # Namespace prefixes are not fixed. Look for any prefixed report
            # element instead of treating an arbitrary '<' as XML.
            match = re.search(rb"<[A-Za-z_][A-Za-z0-9_.-]*:report(?:\s|>)", output)
            if not match:
                return None
            starts = [match.start()]

        start = min(starts)
        end_match = _REPORT_END_RE.search(output, start)
        if not end_match:
            return None
        candidate = output[start : end_match.end()].strip()
        root = cls._parse_xml_root(candidate)
        if root is None or local_name(root).lower() not in {"report", "validationreport"}:
            return None
        return candidate

    @classmethod
    def _extract_format_error_payload(cls, stderr: bytes) -> bytes | None:
        """Recover a report wrapped by KoSIT's ``[Format error!]`` output.

        KoSIT 1.6.2's ``--print`` path passes the complete serialized XML to
        ``MessageFormat``. Report content can therefore trigger a formatting
        exception; KoSIT then writes the otherwise valid XML inside a wrapper
        to stderr. Version 1.0.2 no longer uses ``--print``, but this fallback
        also makes existing/custom launchers readable.
        """

        if not stderr:
            return None
        search_from = 0
        recovered: bytes | None = None
        while True:
            wrapper_start = stderr.find(_FORMAT_ERROR_PREFIX, search_from)
            if wrapper_start < 0:
                break
            payload_start = wrapper_start + len(_FORMAT_ERROR_PREFIX)
            wrapper_end = stderr.find(_FORMAT_ERROR_SUFFIX, payload_start)
            if wrapper_end < 0:
                break
            candidate = stderr[payload_start:wrapper_end]
            payload = cls._extract_xml_payload(candidate)
            if payload is not None:
                recovered = payload
            search_from = wrapper_end + len(_FORMAT_ERROR_SUFFIX)
        return recovered

    @classmethod
    def _read_serialized_report(cls, report_directory: Path, invoice_path: Path) -> bytes | None:
        """Read the report KoSIT writes to its output directory."""

        expected = report_directory / f"{invoice_path.stem}-report.xml"
        candidates: list[Path] = []
        if expected.is_file():
            candidates.append(expected)
        try:
            candidates.extend(
                path
                for path in sorted(
                    report_directory.glob("*.xml"),
                    key=lambda item: item.stat().st_mtime_ns,
                    reverse=True,
                )
                if path not in candidates
            )
        except OSError:
            pass

        for candidate in candidates:
            try:
                payload = candidate.read_bytes()
            except OSError:
                continue
            root = cls._parse_xml_root(payload)
            if root is not None and local_name(root).lower() in {"report", "validationreport"}:
                return payload
        return None

    @staticmethod
    def _report_decision(root: etree._Element) -> tuple[bool | None, str | None]:
        # The actual VARL decision is represented by
        # <rep:assessment><rep:accept/> or <rep:reject/>.
        for element in root.iter():
            if not isinstance(element.tag, str) or local_name(element).lower() != "assessment":
                continue
            for descendant in element.iterdescendants():
                if not isinstance(descendant.tag, str):
                    continue
                lname = local_name(descendant).lower()
                if lname == "reject":
                    return False, "reject"
                if lname == "accept":
                    return True, "accept"

        # Compatibility fallback for report variants with an explicit root
        # valid attribute or textual status.
        valid_raw = root.attrib.get("valid")
        if valid_raw is not None:
            value = valid_raw.strip().lower()
            if value in {"true", "1", "yes"}:
                return True, value
            if value in {"false", "0", "no"}:
                return False, value

        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            lname = local_name(element).lower()
            if lname in {"accepted", "acceptrecommendation", "acceptance", "status"}:
                value = (clean_text(element) or "").strip().lower()
                if value in {"true", "yes", "accepted", "accept", "valid", "ok", "success"}:
                    return True, value
                if value in {"false", "no", "rejected", "reject", "invalid", "failed", "error"}:
                    return False, value
        return None, None

    @classmethod
    def _parse_report(
        cls,
        report_bytes: bytes | None,
    ) -> tuple[list[dict[str, Any]], bool | None, str | None, bool]:
        root = cls._parse_xml_root(report_bytes)
        if root is None:
            return [], None, None, False

        root_name = local_name(root).lower()
        root_namespace = (namespace_uri(root) or "").strip().lower()
        is_validator_report = root_name in {"report", "validationreport"} and (
            "validator" in root_namespace or "varl" in root_namespace or root_namespace == ""
        )
        if not is_validator_report:
            return [], None, None, False

        decision, assessment = cls._report_decision(root)
        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for element in root.iter():
            if len(findings) >= 500:
                break
            if not isinstance(element.tag, str):
                continue
            lname = local_name(element).lower()
            attrs = {key.split("}")[-1].lower(): value for key, value in element.attrib.items()}
            severity_raw = (
                attrs.get("severity") or attrs.get("level") or attrs.get("flag") or attrs.get("class") or ""
            ).lower()
            interesting_name = any(token in lname for token in ("error", "warning", "assert", "message", "notice"))
            interesting_severity = any(
                token in severity_raw for token in ("fatal", "error", "warning", "warn", "info", "information")
            )
            if not interesting_name and not interesting_severity:
                continue

            text = " ".join(" ".join(element.itertext()).split())
            if not text or len(text) < 3:
                continue
            text = text[:2000]
            severity = "info"
            if any(token in severity_raw for token in ("fatal", "error")) or "error" in lname or "failed" in lname:
                severity = "error"
            elif "warn" in severity_raw or "warning" in lname:
                severity = "warning"
            rule_id = (
                attrs.get("id") or attrs.get("test") or attrs.get("rule") or attrs.get("code") or local_name(element)
            )
            key = (rule_id, text)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                {
                    "id": rule_id[:200],
                    "severity": severity,
                    "title": "KoSIT-Prüfmeldung",
                    "message": text,
                    "location": attrs.get("location") or attrs.get("xpath") or attrs.get("context"),
                    "actual": None,
                    "expected": None,
                    "source": "KoSIT Validator",
                }
            )
        return findings, decision, assessment, True

    @staticmethod
    def _not_executed(
        state: dict[str, Any],
        *,
        summary: str,
        message: str | None = None,
        finding_id: str = "KOSIT-CONFIG",
        exit_code: int | None = None,
        technical_output: str | None = None,
    ) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        if message:
            findings.append(
                {
                    "id": finding_id,
                    "severity": "warning",
                    "title": "KoSIT-Prüfung wurde nicht ausgeführt",
                    "message": message[:4000],
                    "location": None,
                    "actual": str(exit_code) if exit_code is not None else None,
                    "expected": "Ausführbares Standalone-JAR, gültige KoSIT-Konfiguration und XML-Prüfbericht",
                    "source": "KoSIT-Anbindung",
                }
            )
        return {
            **state,
            "executed": False,
            "accepted": None,
            "exit_code": exit_code,
            "summary": summary,
            "findings": findings,
            "raw_report": None,
            "technical_output": technical_output,
            "report_source": None,
        }

    @staticmethod
    def _looks_like_startup_failure(text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in TECHNICAL_START_PATTERNS)

    def validate(self, xml_bytes: bytes, filename: str) -> dict[str, Any]:
        state = self.configuration_state()
        if not state["configured"]:
            detail = " ".join(state.get("problems") or [])
            return self._not_executed(
                state,
                summary=f"Offizielle KoSIT-Prüfung ist nicht konfiguriert. {detail}".strip(),
                message=detail or None,
            )

        with tempfile.TemporaryDirectory(prefix="einvoice-kosit-") as temp_dir:
            temp_path = Path(temp_dir)
            invoice_path = temp_path / Path(filename).name
            if invoice_path.suffix.lower() != ".xml":
                invoice_path = invoice_path.with_suffix(".xml")
            invoice_path.write_bytes(xml_bytes)

            report_directory = temp_path / "reports"
            report_directory.mkdir()

            command = [
                self.settings.kosit_java_bin,
                "-jar",
                str(self.settings.kosit_validator_jar),
            ]
            for scenario in self.settings.kosit_scenarios:
                command.extend(["-s", str(scenario)])
            for repository in self.settings.kosit_repositories:
                command.extend(["-r", str(repository)])

            # KoSIT serializes a report for every check. Reading that file is
            # more reliable than '-p/--print', whose implementation in KoSIT
            # 1.6.2 can produce a '[Format error!]' wrapper on stderr.
            command.extend(["-o", str(report_directory), str(invoice_path)])

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self.settings.kosit_timeout_seconds,
                    check=False,
                    cwd=temp_dir,
                    creationflags=WINDOWS_SUBPROCESS_CREATION_FLAGS,
                )
            except subprocess.TimeoutExpired:
                return self._not_executed(
                    state,
                    summary=f"KoSIT-Prüfung wurde nach {self.settings.kosit_timeout_seconds} Sekunden abgebrochen.",
                    message="Zeitüberschreitung beim Start oder bei der Ausführung des KoSIT-Validators.",
                    finding_id="KOSIT-TIMEOUT",
                )
            except OSError as exc:
                return self._not_executed(
                    state,
                    summary=f"KoSIT-Validator konnte nicht gestartet werden: {exc}",
                    message=str(exc),
                    finding_id="KOSIT-START",
                )

            report_payload = self._read_serialized_report(report_directory, invoice_path)
            report_source: str | None = "file" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_xml_payload(completed.stdout)
                report_source = "stdout" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_format_error_payload(completed.stderr)
                report_source = "stderr-format-error" if report_payload is not None else None
            if report_payload is None:
                report_payload = self._extract_xml_payload(completed.stderr)
                report_source = "stderr" if report_payload is not None else None

            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            findings, report_decision, assessment, valid_report = self._parse_report(report_payload)
            technical_output = "\n".join(part for part in (stderr, stdout if not valid_report else "") if part).strip()

            # A Java/configuration failure without a valid report says nothing
            # about the invoice and must never be shown as a rejection.
            if not valid_report:
                diagnostic = technical_output or (
                    f"Der Prozess endete mit Rückgabecode {completed.returncode}, ohne einen auswertbaren "
                    "KoSIT-XML-Bericht zu liefern."
                )
                if self._looks_like_startup_failure(diagnostic):
                    summary = (
                        "KoSIT-Prüfung wurde wegen einer technischen Start- oder "
                        "JAR-Konfigurationsstörung nicht ausgeführt."
                    )
                else:
                    summary = (
                        "KoSIT-Prüfung lieferte keinen auswertbaren XML-Prüfbericht und wurde daher "
                        "nicht als Rechnungsprüfung gewertet."
                    )
                return self._not_executed(
                    state,
                    summary=summary,
                    message=diagnostic,
                    finding_id="KOSIT-EXEC",
                    exit_code=completed.returncode,
                    technical_output=technical_output or None,
                )

        # The explicit assessment in the VARL XML report is authoritative. The
        # process return code remains a fallback for custom/older reports.
        accepted = report_decision if report_decision is not None else completed.returncode == 0

        raw_report = report_payload.decode("utf-8", errors="replace") if report_payload else None
        if raw_report and len(raw_report) > 2_000_000:
            raw_report = raw_report[:2_000_000] + "\n<!-- Bericht für die Anzeige gekürzt -->"

        summary = (
            "KoSIT-Prüfung erfolgreich: Rechnung wurde akzeptiert."
            if accepted
            else "KoSIT-Prüfung abgeschlossen: Rechnung wurde abgelehnt."
        )
        if assessment:
            summary += f" Bewertung im Bericht: {assessment}."
        if report_source == "stderr-format-error":
            summary += " Der XML-Bericht wurde aus einer KoSIT-Formatfehler-Ausgabe wiederhergestellt."

        exit_code_accepts = completed.returncode == 0
        if report_decision is not None and exit_code_accepts != report_decision:
            findings.append(
                {
                    "id": "KOSIT-RESULT-MISMATCH",
                    "severity": "warning",
                    "title": "KoSIT-Bericht und Prozess-Rückgabecode widersprechen sich",
                    "message": (
                        f"Der XML-Bericht bewertet die Rechnung als {'akzeptiert' if report_decision else 'abgelehnt'}, "
                        f"der Prozess endete jedoch mit Rückgabecode {completed.returncode}. "
                        "Für die Anzeige wurde die ausdrückliche Bewertung im XML-Bericht verwendet."
                    ),
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "0 bei Annahme, ungleich 0 bei Ablehnung",
                    "source": "KoSIT Validator",
                }
            )

        if not accepted and not findings:
            findings.append(
                {
                    "id": "KOSIT-REJECT",
                    "severity": "error",
                    "title": "KoSIT-Validator hat die Rechnung abgelehnt",
                    "message": "Der KoSIT-Prüfbericht enthält eine Ablehnungsentscheidung ohne separat extrahierte Einzelmeldung.",
                    "location": None,
                    "actual": str(completed.returncode),
                    "expected": "0 bzw. Annahmeentscheidung im KoSIT-Bericht",
                    "source": "KoSIT Validator",
                }
            )

        clean_technical_output = stderr or None
        if (
            clean_technical_output
            and "[Format error!]" in clean_technical_output
            and report_source == "stderr-format-error"
        ):
            clean_technical_output = None

        return {
            **state,
            "executed": True,
            "accepted": accepted,
            "exit_code": completed.returncode,
            "summary": summary,
            "findings": findings,
            "raw_report": raw_report,
            "technical_output": clean_technical_output,
            "report_source": report_source,
        }
