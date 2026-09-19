from __future__ import annotations

import asyncio
import json
import logging

import h11
import pytest
from uvicorn.protocols.http.flow_control import FlowControl
from uvicorn.protocols.http.h11_impl import RequestResponseCycle as H11Cycle
from uvicorn.protocols.http.httptools_impl import RequestResponseCycle as HttpToolsCycle

from app.processing.budgets import ProcessingError
from tests.test_http_upload import BODY, request_scope
from tests.test_processing_observation_api import OBSERVATION_ID, ObservedLease, Owner, application, headers


class MemoryTransport(asyncio.Transport):
    """Record real protocol bytes without opening a socket or listener."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.closed = False

    def write(self, data):
        assert not self.closed
        self.writes.append(bytes(data))

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


class CycleLease(ObservedLease):
    def __init__(self, *, error=None):
        super().__init__(error=error)
        self.upload = None
        self.cleaned = False
        self.release_count = 0

    async def run(self, upload, settings):
        self.upload = upload
        assert bytes(upload.payload) == b"<test/>"
        try:
            return await super().run(upload, settings)
        finally:
            self.cleaned = True

    def release(self):
        if self.upload is not None:
            assert self.cleaned
            with pytest.raises(ValueError):
                bytes(self.upload.payload)
        self.release_count += 1
        super().release()


def protocol_cycle(backend, scope, body, transport, on_response):
    client = h11.Connection(h11.CLIENT)
    request_bytes = b"".join(
        [
            client.send(h11.Request(method=b"POST", target=b"/api/xml", headers=scope["headers"])),
            client.send(h11.Data(data=body)),
            client.send(h11.EndOfMessage()),
        ]
    )
    connection = h11.Connection(h11.SERVER)
    connection.receive_data(request_bytes)
    while connection.next_event() is not h11.NEED_DATA:
        pass
    event = asyncio.Event()
    options = {
        "scope": scope,
        "transport": transport,
        "flow": FlowControl(transport),
        "logger": logging.getLogger("uvicorn.error"),
        "access_logger": logging.getLogger("uvicorn.access"),
        "access_log": False,
        "default_headers": [],
        "message_event": event,
        "on_response": on_response,
    }
    if backend == "h11":
        cycle = H11Cycle(conn=connection, **options)
    else:
        cycle = HttpToolsCycle(expect_100_continue=False, keep_alive=True, **options)
    # This is the state a protocol supplies once its complete request has arrived.
    cycle.body = bytearray(body)
    cycle.more_body = False
    event.set()
    return cycle, client


@pytest.mark.parametrize("backend", ["h11", "httptools"])
@pytest.mark.parametrize("outcome", ["success", "processing_error", "upload_error"])
def test_real_uvicorn_response_completion_is_not_a_transport_failure(backend, outcome):
    async def scenario():
        initial_tasks = asyncio.all_tasks()
        error = ProcessingError(422, "processing_limit_error", "Synthetische Begrenzung.")
        lease = CycleLease(error=error if outcome == "processing_error" else None)
        owner = Owner(lease=lease)
        body = b"broken" if outcome == "upload_error" else BODY
        scope = request_scope(headers=headers((b"content-length", str(len(body)).encode("ascii"))))
        transport = MemoryTransport()
        completions = []
        cycle, client = protocol_cycle(backend, scope, body, transport, lambda: completions.append(True))
        sent = []
        received = []

        async def receive():
            message = await cycle.receive()
            received.append(message)
            return message

        async def send(message):
            sent.append(message)
            await cycle.send(message)

        await asyncio.wait_for(application(owner)(scope, receive, send), timeout=2)

        # Uvicorn's final body wakes receive() as http.disconnect while the actual
        # transport remains connected. Do not synthesize or delay that notification.
        assert cycle.response_started and cycle.response_complete
        assert not cycle.disconnected and not transport.closed
        assert completions == [True]
        if outcome != "upload_error":
            assert received[-1] == {"type": "http.disconnect"}
        assert await cycle.receive() == {"type": "http.disconnect"}
        assert sum(message["type"] == "http.response.start" for message in sent) == 1
        assert sent[-1]["type"] == "http.response.body" and not sent[-1].get("more_body", False)

        client.receive_data(b"".join(transport.writes))
        response = client.next_event()
        assert isinstance(response, h11.Response)
        assert response.status_code == {"success": 200, "processing_error": 422, "upload_error": 400}[outcome]
        received_body = bytearray()
        while isinstance(message := client.next_event(), h11.Data):
            received_body.extend(message.data)
        assert isinstance(message, h11.EndOfMessage)
        assert dict(response.headers)[b"content-length"] == str(len(received_body)).encode("ascii")
        if outcome == "success":
            assert received_body == b"<test/>"
        else:
            expected = "processing_limit_error" if outcome == "processing_error" else "multipart_input_error"
            assert json.loads(received_body)["type"] == expected

        assert owner.acquisitions == [{"observation_id": OBSERVATION_ID, "operation": "export_xml"}]
        assert lease.released and lease.release_count == 1
        assert (lease.upload is None) == (outcome == "upload_error")
        assert asyncio.all_tasks() == initial_tasks
        expected_phases = ["upload_failed"] if outcome == "upload_error" else []
        assert lease.events == [*expected_phases, "response_sending", "response_send_complete"]

    asyncio.run(scenario())
