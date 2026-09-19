from __future__ import annotations

import asyncio
import io
import struct

import pytest

from app.processing.protocol import (
    CONTROL_LIMIT,
    DATA_LIMIT,
    FrameKind,
    ProtocolError,
    decode_control,
    read_frame,
    read_frame_async,
    read_payload,
    write_control,
    write_frame,
    write_payload,
)


@pytest.mark.parametrize("kind,length", [(1, CONTROL_LIMIT + 1), (2, DATA_LIMIT + 1), (3, 1), (255, 0)])
def test_rejects_frame_length_before_reading_payload(kind: int, length: int) -> None:
    class HeaderOnly(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert self.tell() < 5, "Oversized payload must never be requested"
            return super().read(size)

    with pytest.raises(ProtocolError):
        read_frame(HeaderOnly(struct.pack("!BI", kind, length)))


@pytest.mark.parametrize("raw", [b"", b"\x01", struct.pack("!BI", 1, 3) + b"{}"])
def test_truncated_frames_are_not_success(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        read_frame(io.BytesIO(raw))


@pytest.mark.parametrize(
    "raw", [b'{"status":"ok","status":"error"}', b"[]", b'{"size":NaN}', b"\xff", b"{" + b" " * CONTROL_LIMIT]
)
def test_control_input_rejects_ambiguous_non_object_non_finite_or_oversize_json(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_control(raw)


def test_payload_preserves_binary_bytes_and_requires_explicit_end() -> None:
    payload = bytes(range(256)) * 513
    stream = io.BytesIO()
    write_control(stream, {"version": 1, "size": len(payload)})
    write_payload(stream, memoryview(payload))
    stream.seek(0)
    kind, header = read_frame(stream)
    assert kind is FrameKind.CONTROL
    assert decode_control(header) == {"version": 1, "size": len(payload)}
    assert read_payload(stream, len(payload), maximum=len(payload)) == payload
    with pytest.raises(ProtocolError):
        read_payload(io.BytesIO(stream.getvalue()[5 + len(header) : -5]), len(payload), maximum=len(payload))


def test_short_intermediate_frames_cannot_amplify_message_count() -> None:
    stream = io.BytesIO()
    write_frame(stream, FrameKind.DATA, b"x")
    stream.seek(0)
    with pytest.raises(ProtocolError):
        read_payload(stream, DATA_LIMIT + 1, maximum=DATA_LIMIT + 1)


@pytest.mark.parametrize("length", [-1, True, 4])
def test_payload_length_rejected_before_any_read(length: int) -> None:
    class NoRead(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            pytest.fail("Invalid announced length must not read input")

    with pytest.raises(ProtocolError):
        read_payload(NoRead(), length, maximum=3)


def test_async_framing_checks_before_allocation_and_handles_fragmentation() -> None:
    async def run() -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(struct.pack("!BI", 2, DATA_LIMIT + 1))
        with pytest.raises(ProtocolError):
            await read_frame_async(stream)
        stream = asyncio.StreamReader()
        raw = struct.pack("!BI", 1, 2) + b"{}"
        for byte in raw:
            stream.feed_data(bytes([byte]))
        stream.feed_eof()
        assert await read_frame_async(stream) == (FrameKind.CONTROL, b"{}")

    asyncio.run(run())
