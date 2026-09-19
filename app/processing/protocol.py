"""Length-bounded, non-object IPC for private processing roles.

This module deliberately imports neither application settings nor invoice parsers.
The caller owns the protocol state, operation binding, deadlines and process tree.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Mapping
from enum import IntEnum
from typing import BinaryIO

VERSION = 2
CONTROL_LIMIT = 16 * 1024
DATA_LIMIT = 64 * 1024
_PREFIX = struct.Struct("!BI")


class ProtocolError(ValueError):
    """Invalid, incomplete or over-budget private process message."""


class FrameKind(IntEnum):
    CONTROL = 1
    DATA = 2
    END = 3


def _frame_header(raw: bytes) -> tuple[FrameKind, int]:
    try:
        number, length = _PREFIX.unpack(raw)
        kind = FrameKind(number)
    except (ValueError, struct.error) as exc:
        raise ProtocolError("Ungültiger Protokollrahmen.") from exc
    maximum = {FrameKind.CONTROL: CONTROL_LIMIT, FrameKind.DATA: DATA_LIMIT, FrameKind.END: 0}[kind]
    if length > maximum or (length == 0 and kind is not FrameKind.END):
        raise ProtocolError("Unzulässige Protokollrahmenlänge.")
    return kind, length


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    parts: list[bytes] = []
    remaining = size
    while remaining:
        part = stream.read(remaining)
        if not part or len(part) > remaining:
            raise ProtocolError("Unvollständige Prozessantwort.")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def read_frame(stream: BinaryIO) -> tuple[FrameKind, bytes]:
    kind, length = _frame_header(_read_exact(stream, _PREFIX.size))
    return kind, _read_exact(stream, length)


async def read_frame_async(stream: asyncio.StreamReader) -> tuple[FrameKind, bytes]:
    try:
        kind, length = _frame_header(await stream.readexactly(_PREFIX.size))
        return kind, await stream.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("Unvollständige Prozessantwort.") from exc


def write_frame(stream: BinaryIO, kind: FrameKind, payload: bytes | memoryview = b"") -> None:
    header = _PREFIX.pack(kind, len(payload))
    _frame_header(header)
    for data in (memoryview(header), memoryview(payload)):
        while data:
            count = stream.write(data)
            if count is None or count <= 0 or count > len(data):
                raise ProtocolError("Prozesskanal wurde geschlossen.")
            data = data[count:]
    stream.flush()


async def write_frame_async(stream: asyncio.StreamWriter, kind: FrameKind, payload: bytes | memoryview = b"") -> None:
    header = _PREFIX.pack(kind, len(payload))
    _frame_header(header)
    stream.write(header)
    stream.write(payload)
    await stream.drain()


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ProtocolError("Mehrdeutige Prozessmetadaten.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ProtocolError(f"Unzulässige numerische Prozessmetadaten: {value}.")


def decode_control(payload: bytes) -> dict[str, object]:
    if not 0 < len(payload) <= CONTROL_LIMIT:
        raise ProtocolError("Zu große Prozessmetadaten.")
    try:
        value = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Ungültige Prozessmetadaten.") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Prozessmetadaten müssen ein Objekt sein.")
    return value


def encode_control(value: Mapping[str, object]) -> bytes:
    try:
        payload = json.dumps(dict(value), ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError("Ungültige Prozessmetadaten.") from exc
    if len(payload) > CONTROL_LIMIT:
        raise ProtocolError("Zu große Prozessmetadaten.")
    return payload


def write_control(stream: BinaryIO, value: Mapping[str, object]) -> None:
    write_frame(stream, FrameKind.CONTROL, encode_control(value))


def read_control(stream: BinaryIO) -> dict[str, object]:
    kind, payload = read_frame(stream)
    if kind is not FrameKind.CONTROL:
        raise ProtocolError("Prozessmetadaten erwartet.")
    return decode_control(payload)


def payload_length(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError("Unzulässige Gesamtlänge der Prozessdaten.")
    return value


def read_payload(stream: BinaryIO, length: object, *, maximum: int) -> bytes:
    length = payload_length(length, maximum)
    payload = bytearray(length)
    offset = 0
    while offset < length:
        kind, frame = read_frame(stream)
        if kind is not FrameKind.DATA or len(frame) != min(DATA_LIMIT, length - offset):
            raise ProtocolError("Unvollständige oder unzulässige Prozessdaten.")
        payload[offset : offset + len(frame)] = frame
        offset += len(frame)
    if read_frame(stream) != (FrameKind.END, b""):
        raise ProtocolError("Abschluss der Prozessdaten fehlt.")
    return bytes(payload)


def write_payload(stream: BinaryIO, payload: bytes | memoryview) -> None:
    view = memoryview(payload)
    for offset in range(0, len(view), DATA_LIMIT):
        write_frame(stream, FrameKind.DATA, view[offset : offset + DATA_LIMIT])
    write_frame(stream, FrameKind.END)


async def write_payload_async(stream: asyncio.StreamWriter, payload: bytes | memoryview) -> None:
    view = memoryview(payload)
    for offset in range(0, len(view), DATA_LIMIT):
        await write_frame_async(stream, FrameKind.DATA, view[offset : offset + DATA_LIMIT])
    await write_frame_async(stream, FrameKind.END)


async def read_payload_chunks_async(stream: asyncio.StreamReader, length: int, *, maximum: int) -> list[bytes]:
    remaining = payload_length(length, maximum)
    chunks: list[bytes] = []
    while remaining:
        kind, frame = await read_frame_async(stream)
        if kind is not FrameKind.DATA or len(frame) != min(DATA_LIMIT, remaining):
            raise ProtocolError("Unvollständige oder unzulässige Prozessdaten.")
        chunks.append(frame)
        remaining -= len(frame)
    if await read_frame_async(stream) != (FrameKind.END, b""):
        raise ProtocolError("Abschluss der Prozessdaten fehlt.")
    return chunks
