"""Actual POSIX Uvicorn source CLI shutdown, with kernel-bound native roles."""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.processing.native import ExitBindings, child_environment

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native POSIX SIGTERM/reload lifecycle")
HELPER = Path(__file__).with_name("processing_shutdown_helper.py")


def wait_file(path, process, deadline, log):
    while not path.exists():
        assert process.poll() is None, log.read_text()
        assert time.monotonic() < deadline, f"Missing bounded observer: {path.name}\n{log.read_text()}"
        time.sleep(0.01)
    return json.loads(path.read_text())


def health(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        return response.status == 200 and json.loads(response.read(4096))["status"] == "ok"
    except OSError:
        return False
    finally:
        connection.close()


@pytest.mark.parametrize("mode", ["signal", "reload"])
def test_source_shutdown_ends_every_bound_role_after_real_input_ack(mode, tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    trigger = tmp_path / "synthetic_reload_trigger.py"
    trigger.write_text("# synthetic initial revision\n")
    log = tmp_path / "server.log"
    request = http.client.HTTPConnection("127.0.0.1", port, timeout=25)
    responses = []

    def upload():
        try:
            body = (
                b'--test\r\nContent-Disposition: form-data; name="file"; filename="x.xml"\r\n\r\n<x/>\r\n--test--\r\n'
            )
            request.request("POST", "/api/xml", body, {"Content-Type": "multipart/form-data; boundary=test"})
            response = request.getresponse()
            responses.append(response.status)
            response.read(65536)
        except (OSError, http.client.HTTPException):
            responses.append("disconnected")
        finally:
            request.close()

    thread = threading.Thread(target=upload, daemon=True)
    binding = None
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-I", str(HELPER), mode, str(tmp_path), str(port)],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=child_environment(),
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 12
            while not health(port):
                assert process.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.02)
            thread.start()
            active = wait_file(tmp_path / "active.json", process, deadline, log)
            assert active["input_acknowledged"] is True and thread.is_alive()
            pids = active["pids"]
            backend = active["backend_pid"]
            assert len(set(pids)) == len(pids) == (3 if sys.platform == "darwin" else 2)
            assert all(type(pid) is int and pid > 1 for pid in [backend, *pids])
            binding = ExitBindings([backend, *pids])
            if mode == "signal":
                assert backend == process.pid
                process.send_signal(signal.SIGTERM)
            else:
                assert backend != process.pid
                trigger.write_text("# synthetic changed revision\n")
            stopped = wait_file(tmp_path / f"shutdown-{backend}.json", process, time.monotonic() + 5, log)
            assert binding.wait(stopped["monotonic"] + 6), "roles exceeded five-second cleanup plus Uvicorn latency"
            assert time.monotonic() - stopped["monotonic"] < 6
            thread.join(1)
            assert not thread.is_alive() and responses and 200 not in responses
            if mode == "reload":
                deadline = time.monotonic() + 10
                while len(list(tmp_path.glob("backend-*.json"))) < 2 or not health(port):
                    assert process.poll() is None, log.read_text()
                    assert time.monotonic() < deadline, "reloaded backend failed to serve health"
                    time.sleep(0.02)
                process.send_signal(signal.SIGTERM)
            process.wait(timeout=6)
        finally:
            # The harness is its own unreaped session/group leader, never a
            # numeric PID learned from an unrelated process. Kill only this
            # fixed test group on failure; its native workers remain guarded.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            request.close()
            if thread.ident is not None:
                thread.join(2)
            if binding is not None:
                assert binding.wait(time.monotonic() + 3), "bounded test group left live processing roles"
                binding.close()
