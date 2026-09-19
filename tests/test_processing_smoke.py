from __future__ import annotations

import io
import json
import xml.etree.ElementTree as ET

import pytest

from scripts import processing_smoke as smoke


def test_exact_upload_ceiling_fixture_is_fixed_synthetic_xml():
    payload = smoke.maximum_xml()
    assert len(payload) == 25 * 1024**2
    assert payload.startswith(b'<?xml version="1.0"?>')
    assert payload.endswith(b"</synthetic>")


@pytest.mark.parametrize("syntax", ["cii", "ubl"])
def test_large_examples_have_fixed_unique_lines_and_consistent_totals(syntax):
    payload = smoke.large_example(syntax)
    root = ET.fromstring(payload)
    lines = [
        element
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == ("IncludedSupplyChainTradeLineItem" if syntax == "cii" else "InvoiceLine")
    ]
    assert len(lines) == smoke.LARGE_LINES == 128
    assert len(payload) < 256 * 1024
    identifiers = [
        next(
            iter(
                line.iter(
                    "{" + smoke.NS["ram" if syntax == "cii" else "cbc"] + "}" + ("LineID" if syntax == "cii" else "ID")
                )
            )
        ).text
        for line in lines
    ]
    assert identifiers == [str(index) for index in range(1, 129)]
    assert b"12800.00" in payload and b"2432.00" in payload and b"15232.00" in payload


def test_case_inventory_has_no_user_selected_commands_or_fixture_paths():
    assert set(smoke.CASES) == {"small", "xml_25mib", "cii_large", "ubl_large", "hybrid_pdf", "parallel"}
    assert all(0 < deadline <= 60 for deadline in smoke.CASES.values())
    with pytest.raises(ValueError):
        smoke.large_example("untrusted")


def test_export_validation_requires_actual_byte_identity():
    record = smoke.validate_output("export_xml", b"<a/>", b"<a/>", "application/xml", None)
    assert record["byte_identical"] is True
    with pytest.raises(ValueError):
        smoke.validate_output("export_xml", b"<a/>", b"<b/>", "application/xml", None)


def test_analysis_validation_requires_expected_synthetic_lines():
    output = json.dumps({"schema_version": 2, "lines": [{}, {}]}).encode()
    assert smoke.validate_output("analyze", b"<a/>", output, "application/json", 2)["line_count"] == 2
    with pytest.raises(ValueError):
        smoke.validate_output("analyze", b"<a/>", output, "application/json", 3)


def test_forced_stop_and_missing_cleanup_cannot_pass():
    good = {"passed": True, "cleanup_confirmed": True, "active_after": 0}
    assert smoke.successful_child(good, returncode=0, forced_stop=None)
    assert not smoke.successful_child(good, returncode=0, forced_stop="deadline")
    assert not smoke.successful_child(good, returncode=1, forced_stop=None)
    assert not smoke.successful_child(good | {"cleanup_confirmed": False}, returncode=0, forced_stop=None)
    assert not smoke.successful_child(good | {"active_after": 1}, returncode=0, forced_stop=None)


def test_evidence_is_exclusive_and_does_not_replace_old_results(tmp_path):
    path = tmp_path / "evidence.json"
    smoke.write_json(path, {"passed": False})
    with pytest.raises(FileExistsError):
        smoke.write_json(path, {"passed": True})
    assert json.loads(path.read_text()) == {"passed": False}


def test_interrupted_outer_wait_still_kills_and_reaps_only_its_harness(monkeypatch, tmp_path):
    class Child:
        stdout = io.BytesIO(b"{}")
        stderr = io.BytesIO()
        killed = False
        waits = 0

        def kill(self):
            self.killed = True

        def wait(self, *, timeout):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            assert self.killed and timeout <= 5
            return -9

    child = Child()
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: child)
    with pytest.raises(KeyboardInterrupt):
        smoke.run_bounded("small", tmp_path / "interrupted")
    assert child.killed and child.waits == 2
    assert child.stdout.closed and child.stderr.closed


@pytest.mark.parametrize("platform,raw,expected", [("darwin", 4096, 4096), ("linux", 4096, 4194304)])
def test_native_wait4_rss_has_explicit_platform_units(platform, raw, expected):
    assert smoke.rss_bytes(raw, platform) == expected


def test_native_memory_uses_peak_fields_not_sample_or_virtual_address_space():
    def query(handle, output, size):
        assert handle == 123 and size == output._obj.cb
        value = output._obj
        value.peak_working_set = 900
        value.working_set = 100
        value.peak_commit = 2000
        value.private = 300
        return 1

    result = smoke.windows_memory_counters(123, query=query)
    assert result["peak_working_set_bytes"] == 900
    assert result["peak_private_commit_bytes"] == 2000
    assert result["sample_working_set_bytes"] == 100
    assert result["sample_private_commit_bytes"] == 300
    assert result["limit_verified"] is False
    with pytest.raises(OSError):
        smoke.windows_memory_counters(123, query=lambda *args: 0)


def test_wait4_observer_preserves_pid_flags_and_normal_reaping(monkeypatch):
    from types import SimpleNamespace

    child = SimpleNamespace(pid=123, _try_wait=lambda flags: (123, 0))
    calls = []

    def wait4(pid, flags):
        calls.append((pid, flags))
        return pid, 9, SimpleNamespace(ru_maxrss=40, ru_utime=0.1, ru_stime=0.2)

    monkeypatch.setattr(smoke.os, "wait4", wait4)
    record = {}
    smoke.observe_process(child, record, platform="linux")
    assert child._try_wait(7) == (123, 9)
    assert calls == [(123, 7)]
    assert record["peak_rss_bytes"] == 40960 and record["status"] == "observed"


def test_early_reaped_process_never_gets_invented_peak(monkeypatch):
    from types import SimpleNamespace

    calls = []
    child = SimpleNamespace(pid=123, _try_wait=lambda flags: calls.append(flags) or (123, 0))

    def gone(pid, flags):
        raise ChildProcessError

    monkeypatch.setattr(smoke.os, "wait4", gone)
    record = {}
    smoke.observe_process(child, record, platform="darwin")
    assert child._try_wait(1) == (123, 0) and calls == [1]
    assert record["status"] == "unavailable_already_reaped"
    assert "peak_rss_bytes" not in record


def test_ready_observer_preserves_real_event_and_marks_first_observation():
    import threading

    event = threading.Event()
    record = {}
    smoke.observe_ready(event, record, started=smoke.time.monotonic())
    event.set()
    first = record["ready_seconds"]
    event.set()
    assert event.is_set() and record["ready_seconds"] == first and first >= 0


def test_metric_validation_failure_preserves_already_reaped_status(monkeypatch):
    from types import SimpleNamespace

    child = SimpleNamespace(pid=123, _try_wait=lambda flags: (123, 0))
    monkeypatch.setattr(smoke.os, "wait4", lambda pid, flags: (pid, 17, SimpleNamespace(ru_maxrss=-1)))
    record = {}
    smoke.observe_process(child, record, platform="linux")
    assert child._try_wait(0) == (123, 17)
    assert record["status"] == "unavailable_metric_validation"


def test_windows_counter_failure_never_prevents_owned_handle_close(monkeypatch):
    import threading
    from types import SimpleNamespace

    closed = []
    child = SimpleNamespace(pid=123, _lock=threading.RLock(), _handle=42, close=lambda: closed.append(42))

    def invalid(handle):
        raise ValueError("synthetic counter failure")

    monkeypatch.setattr(smoke, "windows_memory_counters", invalid)
    record = {}
    smoke.observe_process(child, record, platform="win32")
    child.close()
    assert closed == [42] and record["status"] == "unavailable_final_query"


def test_hybrid_fixture_has_exact_embedded_xml_and_explicit_filter_stages():
    import zlib
    from io import BytesIO

    from pypdf import PdfReader

    pdf, xml, sizes = smoke.hybrid_example()
    assert len(xml) == 2 * 1024**2
    assert xml.startswith(b"<?xml") and b"SYNTHETIC_HYBRID_CALIBRATION" in xml
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 1 and reader.attachments["factur-x.xml"] == [xml]
    embedded = next(reader.attachment_list).pdf_object["/EF"]["/F"]
    assert list(embedded["/Filter"]) == ["/ASCIIHexDecode", "/FlateDecode"]
    compressed = zlib.compress(xml)
    assert sizes == {
        "xml_bytes": len(xml),
        "pdf_bytes": len(pdf),
        "ascii_hex_input_bytes": 2 * len(compressed) + 1,
        "after_ascii_hex_bytes": len(compressed),
        "after_flate_bytes": len(xml),
    }
    assert smoke.validate_output("export_xml", xml, xml, "application/xml", None)["byte_identical"]
    with pytest.raises(ValueError):
        smoke.validate_output("export_xml", pdf, xml, "application/xml", None)
