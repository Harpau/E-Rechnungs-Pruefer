"""Real loopback Uvicorn transport, synthetic leases, no parser/Java subprocesses."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic_transport_test_" + "a" * 32
SERVER = r"""
import os, socket, sys, threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import uvicorn
from app import main

# Independent absolute bound even if a test fails to finish or close its client.
timer = threading.Timer(25, lambda: os._exit(97))
timer.daemon = True
timer.start()

class Lease:
    def __init__(self, owner):
        self.owner, self.released = owner, False
    async def run(self, upload, app_settings):
        raise AssertionError("Transport probes must never start invoice processing")
    def release(self):
        if not self.released:
            self.released = True
            self.owner.active -= 1

class Manager:
    budgets = SimpleNamespace(send_seconds=1)
    active = 0
    def try_acquire(self):
        if self.active == 2:
            return None
        self.active += 1
        return Lease(self)
    def startup(self): pass
    def shutdown(self): pass

main.manager = Manager()
main.settings = replace(main.settings, max_upload_bytes=128)
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(16)
Path(sys.argv[1]).write_text(str(listener.getsockname()[1]), encoding="ascii")
config = uvicorn.Config(main.app, log_level="critical", access_log=False, http=sys.argv[2],
                        lifespan="on", timeout_graceful_shutdown=1, timeout_keep_alive=2)
uvicorn.Server(config).run(sockets=[listener])
timer.cancel()
"""


@pytest.fixture(scope="module", params=["h11", "httptools"])
def transport_server(request, tmp_path_factory):
    directory = tmp_path_factory.mktemp("synthetic-http-" + request.param)
    port_file = directory / "port.txt"
    log_file = directory / "server.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("EINVOICE_")}
    env["EINVOICE_API_TOKEN"] = TOKEN
    with log_file.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(port_file), request.param],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=os.name == "posix",
        )
        try:
            deadline = time.monotonic() + 5
            while not port_file.exists():
                assert process.poll() is None, log_file.read_text()
                assert time.monotonic() < deadline, "Bounded HTTP server did not start"
                time.sleep(0.01)
            port = int(port_file.read_text())
            # Socket creation precedes the server loop; one bounded health check waits for readiness.
            with wire(port) as connection:
                connection.send(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                assert connection.response()[0] == 200
            yield port
        finally:
            # This server is never permitted to launch processing children.
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


class Wire:
    def __init__(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.socket.settimeout(3)
        self.file = self.socket.makefile("rb")

    def send(self, payload):
        self.socket.sendall(payload)

    def response(self):
        line = self.file.readline(8193)
        assert line.startswith(b"HTTP/1.1 ") and len(line) <= 8192, line
        status = int(line.split(b" ")[1])
        headers = {}
        for _ in range(64):
            line = self.file.readline(8193)
            assert len(line) <= 8192
            if line == b"\r\n":
                break
            key, separator, value = line.partition(b":")
            assert separator
            headers[key.lower()] = value.strip()
        else:
            raise AssertionError("Unexpected response header count")
        length = int(headers.get(b"content-length", b"0"))
        assert 0 <= length <= 64 * 1024
        return status, headers, self.file.read(length)

    def close(self):
        self.file.close()
        self.socket.close()


@contextmanager
def wire(port):
    connection = Wire(port)
    try:
        yield connection
    finally:
        connection.close()


def upload_headers(*, length, authorized=True, expect=True):
    headers = [
        b"POST /api/xml HTTP/1.1",
        b"Host: localhost",
        b"Content-Type: multipart/form-data; boundary=test",
        b"Content-Length: " + str(length).encode(),
    ]
    if authorized:
        headers.append(b"Authorization: Bearer " + TOKEN.encode())
    if expect:
        headers.append(b"Expect: 100-continue")
    return b"\r\n".join(headers) + b"\r\n\r\n"


def test_native_unauthorized_and_oversized_rejection_precedes_100_continue(transport_server):
    for authorized, length, expected in [(False, 100, 403), (True, 128 + 65536 + 1, 413)]:
        with wire(transport_server) as connection:
            connection.send(upload_headers(length=length, authorized=authorized))
            status, headers, _body = connection.response()
            assert status == expected  # An interim 100 response would fail this assertion.
            assert headers[b"cache-control"] == b"no-store"
            assert headers[b"x-content-type-options"] == b"nosniff"


def test_native_third_upload_gets_503_before_continue_and_health_stays_live(transport_server):
    first, second = Wire(transport_server), Wire(transport_server)
    try:
        for connection in (first, second):
            connection.send(upload_headers(length=100))
            assert connection.response()[0] == 100
        with wire(transport_server) as third:
            third.send(upload_headers(length=100))
            status, headers, _body = third.response()
            assert status == 503
            assert headers[b"retry-after"] == b"65"
        with wire(transport_server) as health:
            health.send(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert health.response()[0] == 200
    finally:
        first.close()
        second.close()
    # EOF must release both synthetic leases. A closed form without a file is 422,
    # not capacity exhaustion, and never reaches the invoice processor.
    deadline = time.monotonic() + 2
    while True:
        with wire(transport_server) as probe:
            empty_form = b"--test--\r\n"
            probe.send(upload_headers(length=len(empty_form), expect=False) + empty_form)
            status, _headers, _body = probe.response()
        if status != 503:
            assert status == 422
            break
        assert time.monotonic() < deadline, "Disconnected uploads retained capacity"
        time.sleep(0.01)


@pytest.mark.parametrize("chunked", [False, True])
def test_native_file_limit_then_keepalive_health(transport_server, chunked):
    body = (
        b'--test\r\nContent-Disposition: form-data; name="file"; filename="x.xml"\r\n\r\n'
        + b"x" * 129
        + b"\r\n--test--\r\n"
    )
    headers = upload_headers(length=len(body), expect=False)
    if chunked:
        headers = headers.replace(b"Content-Length: " + str(len(body)).encode(), b"Transfer-Encoding: chunked")
        transmitted = format(len(body), "x").encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    else:
        transmitted = body
    with wire(transport_server) as connection:
        connection.send(headers + transmitted)
        status, _headers, payload = connection.response()
        assert status == 413 and b"upload_limit_error" in payload
        connection.send(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert connection.response()[0] == 200


def test_native_header_budget_and_duplicate_content_length(transport_server):
    with wire(transport_server) as connection:
        connection.send(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nX-Large: " + b"x" * 8193 + b"\r\n\r\n")
        assert connection.response()[0] == 431
    with wire(transport_server) as connection:
        connection.send(
            upload_headers(length=1).replace(b"Content-Length: 1", b"Content-Length: 1\r\nContent-Length: 2")
        )
        # HTTP-server framing rejection; application 100/200 is never acceptable.
        assert connection.response()[0] == 400
