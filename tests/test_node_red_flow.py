from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FLOW_PATH = PROJECT_ROOT / "docs/examples/node-red-e-rechnungs-pruefer-flow.json"
NODE_TEST_PATH = PROJECT_ROOT / "tests/node_red_flow.test.mjs"


def _flow_nodes() -> list[dict]:
    return json.loads(FLOW_PATH.read_text(encoding="utf-8"))


def test_node_red_flow_is_anonymized_and_contains_no_api_secret() -> None:
    text = FLOW_PATH.read_text(encoding="utf-8")

    assert "EINVOICE_API_TOKEN" in text
    assert "Authorization': `Bearer ${apiToken}`" in text
    assert "api-token.txt" not in text

    email_domains = re.findall(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", text)
    assert email_domains == ["example.invalid"]

    nodes = _flow_nodes()
    accounts = [node for node in nodes if node.get("type") in {"imap-email account", "smtp-email account"}]
    assert accounts
    assert all(str(node["host"]).endswith(".example.invalid") for node in accounts)
    smtp_account = next(node for node in accounts if node["type"] == "smtp-email account")
    assert smtp_account["from"].endswith("@example.invalid")


def test_examples_directory_contains_only_the_canonical_node_red_flow() -> None:
    assert list(FLOW_PATH.parent.glob("*.json")) == [FLOW_PATH]


def test_node_red_flow_has_unique_ids_and_only_valid_wires() -> None:
    nodes = _flow_nodes()
    ids = [node["id"] for node in nodes]

    assert len(ids) == len(set(ids))
    known_ids = set(ids)
    for node in nodes:
        for output in node.get("wires", []):
            assert all(target in known_ids for target in output)


def test_node_red_flow_implements_status_contract_and_safe_acknowledgement() -> None:
    nodes = _flow_nodes()
    by_name = {node.get("name"): node for node in nodes if node.get("name")}

    classifier = by_name["HTTP- und Prüfstatus klassifizieren"]["func"]
    assert "x-einvoice-syntax" in classifier
    assert "x-einvoice-validation-status" in classifier
    assert "x-einvoice-official-status" in classifier
    assert "status === 413 || status === 422" in classifier
    assert "status === 408 || status === 429 || status >= 500" in classifier
    assert "mediaType !== 'application/pdf'" in classifier
    assert "=== '%PDF-'" in classifier
    assert "contentDisposition: 'attachment'" in classifier
    assert "PDF- und Statusvertrag" in classifier

    retry = by_name["Begrenzt wiederholen"]["func"]
    assert "[30000, 120000, 600000]" in retry
    assert "state.lastError = `HTTP-Verbindungsfehler:" in retry
    assert "delete msg.error" in retry
    assert "Math.min(serverDelay, 600000)" in retry
    assert by_name["Retry-Backoff"]["pauseType"] == "delayv"

    http_request = by_name["POST /api/report/pdf"]
    classifier_node = by_name["HTTP- und Prüfstatus klassifizieren"]
    http_error = by_name["HTTP-Verbindungsfehler"]
    retry_node = by_name["Begrenzt wiederholen"]
    assert http_request["senderr"] is True
    assert http_request["followRedirects"] is False
    assert http_request["wires"] == [[classifier_node["id"]]]
    assert http_error["scope"] == [http_request["id"]]
    assert http_error["wires"] == [[retry_node["id"]]]

    sender = by_name["Prüfbericht versenden"]
    acknowledgement = by_name["Erst jetzt quittieren"]
    error_handler = by_name["Technischen Fehler bereinigen"]
    assert sender["wires"][0] == [acknowledgement["id"]]
    assert acknowledgement["id"] not in sender["wires"][1]
    assert acknowledgement["wires"][1] == [error_handler["id"]]
    assert by_name["TECHNISCHER FEHLER – persistent anbinden"]["links"] == []


def test_node_red_flow_integrates_imap_input_and_ack_safely() -> None:
    nodes = _flow_nodes()
    by_name = {node.get("name"): node for node in nodes if node.get("name")}
    imap_inputs = [node for node in nodes if node.get("type") == "imap-email in"]
    imap_accounts = [node for node in nodes if node.get("type") == "imap-email account"]

    assert len(imap_inputs) == 1
    assert len(imap_accounts) == 1
    imap_input = imap_inputs[0]
    imap_account = imap_accounts[0]
    acknowledgement = by_name["Erst jetzt quittieren"]
    attachment_guard = by_name["IMAP-Anhangsdaten prüfen"]
    error_handler = by_name["Technischen Fehler bereinigen"]

    assert imap_input["account"] == acknowledgement["account"] == imap_account["id"]
    assert imap_input["includeAttachments"] is True
    assert imap_input["maxMessageBytes"] == 67_108_864
    assert imap_input["batchSize"] == 5
    assert imap_input["maxInflight"] == 5
    assert imap_input["seenSelection"] == "exclude"
    assert imap_input["deletedSelection"] == "exclude"
    assert imap_input["expungeDeletedFront"] is False
    assert acknowledgement["seenAction"] == "set"
    assert imap_input["wires"][0] == [attachment_guard["id"]]
    assert imap_input["wires"][1] == [error_handler["id"]]

    guard_source = attachment_guard["func"]
    assert "!Array.isArray(msg.email?.attachments)" in guard_source
    assert "return [null, msg]" in guard_source
    assert attachment_guard["wires"] == [
        [by_name["Alle XML/PDF-Kandidaten vorbereiten"]["id"]],
        [error_handler["id"]],
    ]

    flow_tab = next(node for node in nodes if node.get("type") == "tab")
    assert flow_tab["disabled"] is True


def test_node_red_flow_uses_only_configured_local_report_endpoint() -> None:
    nodes = _flow_nodes()
    preparer = next(node for node in nodes if node.get("name") == "Multipart-API-Aufruf vorbereiten")

    assert "http://127.0.0.1:8080/api/report/pdf" in preparer["func"]
    assert "new URL" not in preparer["func"]
    assert "const apiUrlMatch = /^http:\\/\\/(127\\.0\\.0\\.1|localhost)" in preparer["func"]
    assert "apiPort < 1 || apiPort > 65535" in preparer["func"]
    assert "\\/api\\/report\\/pdf$/i.exec(apiUrl)" in preparer["func"]
    assert "String(env.get('EINVOICE_API_TOKEN') || '')" in preparer["func"]
    assert "EINVOICE_API_TOKEN') || '').trim()" not in preparer["func"]
    assert "multipart/form-data" in preparer["func"]
    assert "state.requireKosit === false ? 'false' : 'true'" in preparer["func"]
    assert "${officialValue}" in preparer["func"]
    assert "'Accept': 'application/pdf'" in preparer["func"]
    assert "msg.requestTimeout = 90000" in preparer["func"]
    assert "msg.followRedirects = false" in preparer["func"]
    assert "/^[A-Za-z0-9_-]{32,}$/" in preparer["func"]
    http_request = next(node for node in nodes if node.get("name") == "POST /api/report/pdf")
    direct_proxy = next(node for node in nodes if node.get("type") == "http proxy")
    assert http_request["ret"] == "bin"
    assert http_request["followRedirects"] is False
    assert "requestTimeout" not in http_request
    assert http_request["proxy"] == direct_proxy["id"]
    assert direct_proxy["url"] == "http://127.0.0.1:9"
    assert set(direct_proxy["noproxy"]) == {"127.0.0.1", "localhost"}


def test_node_red_flow_removes_request_timeout_after_http_processing() -> None:
    nodes = _flow_nodes()
    by_name = {node.get("name"): node for node in nodes if node.get("name")}

    for name in (
        "Nächsten Kandidaten wählen",
        "Begrenzt wiederholen",
        "Mailergebnis abschließen",
        "Technischen Fehler bereinigen",
    ):
        assert "delete msg.requestTimeout" in by_name[name]["func"]

    for name in (
        "Multipart-API-Aufruf vorbereiten",
        "HTTP- und Prüfstatus klassifizieren",
        "Nächsten Kandidaten wählen",
        "Begrenzt wiederholen",
        "Mailergebnis abschließen",
        "Technischen Fehler bereinigen",
    ):
        assert "delete msg.followRedirects" in by_name[name]["func"]


def test_node_red_flow_errors_have_timestamps_and_retry_after_is_bounded() -> None:
    nodes = _flow_nodes()
    by_name = {node.get("name"): node for node in nodes if node.get("name")}

    error_nodes = (
        "IMAP-Anhangsdaten prüfen",
        "Multipart-API-Aufruf vorbereiten",
        "HTTP- und Prüfstatus klassifizieren",
        "Begrenzt wiederholen",
        "Mailergebnis abschließen",
        "Technischen Fehler bereinigen",
    )
    assert all("occurredAt" in by_name[name]["func"] for name in error_nodes)

    classifier = by_name["HTTP- und Prüfstatus klassifizieren"]["func"]
    assert "Date.parse(raw)" in classifier
    assert "Math.min(Math.max(target - Date.now(), 0), 600000)" in classifier


def test_node_red_flow_function_nodes_in_real_node_runtime() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js ist lokal nicht installiert")

    completed = subprocess.run(
        [node, "--test", str(NODE_TEST_PATH)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
