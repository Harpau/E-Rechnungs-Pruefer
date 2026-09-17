"""Bounded ASGI upload reception, independent of authentication and job admission.

The caller validates/normalizes headers before authentication, reserves a job
lease before reception, and retains it until processing and response cleanup.
Nothing in this module installs middleware or changes the running application.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields
from time import monotonic
from types import MappingProxyType
from typing import Any, Literal

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError, MultipartParseError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope

Operation = Literal["analyze", "export_xml", "report_html", "report_pdf"]
ReportScope = Literal["readable", "complete"]
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_MEDIA_TYPE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}\Z")
_SINGLETON_HEADERS = frozenset(
    {
        b"host",
        b"content-type",
        b"content-length",
        b"content-encoding",
        b"transfer-encoding",
        b"authorization",
        b"origin",
        b"x-einvoice-ui-revision",
        b"expect",
    }
)
# Parser warnings can contain untrusted header fragments. Keep them out of logs
# without changing the library's process-global logger configuration.
_PARSER_LOGGER = logging.Logger("einvoice.upload.multipart")
_PARSER_LOGGER.disabled = True


class UploadError(Exception):
    def __init__(self, status: int, error_type: str, detail: str | list[dict[str, Any]]) -> None:
        self.status = status
        self.error_type = error_type
        self.type = error_type
        self.detail = detail
        super().__init__(detail if isinstance(detail, str) else "Ungültige Formularfelder.")


def _protocol() -> UploadError:
    return UploadError(400, "multipart_input_error", "Die Uploadanfrage ist unvollständig oder nicht eindeutig.")


def _too_large() -> UploadError:
    return UploadError(413, "upload_limit_error", "Die Uploadanfrage überschreitet die zulässige Größenbegrenzung.")


def _timeout() -> UploadError:
    return UploadError(408, "upload_timeout_error", "Die zulässige Zeit für die Dateiübertragung wurde überschritten.")


def _validation(name: str, kind: str, message: str) -> UploadError:
    return UploadError(422, "request_validation_error", [{"type": kind, "loc": ["body", name], "msg": message}])


@dataclass(frozen=True, slots=True)
class UploadLimits:
    max_upload_bytes: int = 25 * 1024 * 1024
    max_overhead_bytes: int = 64 * 1024
    max_headers: int = 64
    max_header_bytes: int = 16 * 1024
    max_header_value_bytes: int = 8 * 1024
    max_content_type_bytes: int = 1024
    max_part_header_bytes: int = 4096
    max_filename_bytes: int = 1024
    max_field_bytes: int = 64
    feed_bytes: int = 64 * 1024
    total_timeout_seconds: float = 120.0
    idle_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("_seconds"):
                valid = (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                    and value > 0
                )
            else:
                valid = not isinstance(value, bool) and isinstance(value, int) and 0 < value <= sys.maxsize
            if not valid:
                raise ValueError(f"Ungültige Uploadgrenze: {field.name}")
        if self.max_upload_bytes + self.max_overhead_bytes > sys.maxsize or self.feed_bytes > 64 * 1024:
            raise ValueError("Unzulässige Upload- oder Puffergrenze.")

    @property
    def max_body_bytes(self) -> int:
        return self.max_upload_bytes + self.max_overhead_bytes


@dataclass(frozen=True, slots=True)
class UploadOptions:
    official: bool = True
    scope: ReportScope = "readable"


@dataclass(frozen=True, slots=True)
class ReceivedUpload:
    operation: Operation
    filename: str
    media_type: str | None
    options: UploadOptions
    payload: memoryview

    @property
    def size(self) -> int:
        return self.payload.nbytes

    def close(self) -> None:
        """Drop this view's ownership; consumers must release their own slices."""
        self.payload.release()


@dataclass(frozen=True, slots=True)
class UploadOperation:
    operation: Operation
    fields: tuple[str, ...]


UPLOAD_OPERATIONS: Mapping[str, UploadOperation] = MappingProxyType(
    {
        "/api/analyze": UploadOperation("analyze", ("official",)),
        "/api/xml": UploadOperation("export_xml", ()),
        "/api/report": UploadOperation("report_html", ("official", "scope")),
        "/api/report/pdf": UploadOperation("report_pdf", ("official", "scope")),
    }
)


def operation_for_scope(scope: Scope) -> UploadOperation | None:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return None
    path = scope.get("path", "")
    root = scope.get("root_path", "").rstrip("/")
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    return UPLOAD_OPERATIONS.get(path)


def _has_controls(value: bytes | bytearray, *, allow_tab: bool = False) -> bool:
    return any((c < 32 and not (allow_tab and c == 9)) or c == 127 for c in value)


def validate_headers(scope: Scope, limits: UploadLimits) -> tuple[tuple[bytes, bytes], ...]:
    """Return normalized headers for the caller's copied scope BEFORE auth.

    Does not consume receive, allocate an upload buffer or mutate the scope.
    HTTP/2 and HTTP/3 Cookie splitting is normalized before existing auth runs.
    """
    headers: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    cookies: list[bytes] = []
    total = 0
    for key, value in scope.get("headers", []):
        total += len(key) + len(value) + 4
        if (
            len(headers) + len(cookies) >= limits.max_headers
            or total > limits.max_header_bytes
            or len(value) > limits.max_header_value_bytes
        ):
            raise UploadError(
                431, "request_header_limit_error", "Die HTTP-Kopfzeilen überschreiten die zulässige Größe."
            )
        if not _TOKEN.fullmatch(key) or _has_controls(value, allow_tab=True):
            raise _protocol()
        name = key.lower()
        if name in _SINGLETON_HEADERS and name in seen:
            raise _protocol()
        seen.add(name)
        if name == b"cookie":
            cookies.append(value)
        else:
            headers.append((name, value))
    if len(cookies) > 1 and scope.get("http_version") not in {"2", "3"}:
        raise _protocol()
    if cookies:
        cookie = b"; ".join(cookies)
        if len(cookie) > limits.max_header_value_bytes:
            raise UploadError(
                431, "request_header_limit_error", "Die HTTP-Kopfzeilen überschreiten die zulässige Größe."
            )
        # Match Starlette's latin-1 decode and key.strip() after splitting '='.
        # Byte-strip alone misses NBSP/NEL, and stripping the whole pair misses
        # whitespace immediately before '='.
        sessions = sum(
            item.partition("=")[0].strip() == "einvoice_desktop_session" for item in cookie.decode("latin-1").split(";")
        )
        if sessions > 1:
            raise _protocol()
        headers.append((b"cookie", cookie))
    values = dict(headers)
    length = values.get(b"content-length")
    if length is not None and (not re.fullmatch(rb"[0-9]{1,20}", length) or b"transfer-encoding" in values):
        raise _protocol()
    if b"transfer-encoding" in values and values[b"transfer-encoding"].lower().strip() != b"chunked":
        raise _protocol()
    if b"expect" in values and values[b"expect"].lower().strip() != b"100-continue":
        raise _protocol()
    return tuple(headers)


def _parameters(value: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    """Bounded MIME parameter lexer preserving duplicates until rejection."""
    base, separator, rest = value.partition(b";")
    params: dict[bytes, bytes] = {}
    if not separator:
        return base.strip().lower(), params
    offset = 0
    while offset < len(rest):
        while offset < len(rest) and rest[offset] in b" \t":
            offset += 1
        start = offset
        while offset < len(rest) and rest[offset] not in b"=; \t":
            offset += 1
        key = rest[start:offset].lower()
        if not _TOKEN.fullmatch(key) or b"*" in key or key in params:
            raise _protocol()
        while offset < len(rest) and rest[offset] in b" \t":
            offset += 1
        if offset >= len(rest) or rest[offset] != 61:
            raise _protocol()
        offset += 1
        while offset < len(rest) and rest[offset] in b" \t":
            offset += 1
        item = bytearray()
        if offset < len(rest) and rest[offset] == 34:
            offset += 1
            while offset < len(rest) and rest[offset] != 34:
                char = rest[offset]
                if char == 92:
                    offset += 1
                    if offset >= len(rest):
                        raise _protocol()
                    char = rest[offset]
                item.append(char)
                offset += 1
            if offset >= len(rest):
                raise _protocol()
            offset += 1
        else:
            start = offset
            while offset < len(rest) and rest[offset] not in b"; \t":
                offset += 1
            item.extend(rest[start:offset])
            if not _TOKEN.fullmatch(item):
                raise _protocol()
        if _has_controls(item):
            raise _protocol()
        params[key] = bytes(item)
        while offset < len(rest) and rest[offset] in b" \t":
            offset += 1
        if offset < len(rest):
            if rest[offset] != 59 or offset == len(rest) - 1:
                raise _protocol()
            offset += 1
    return base.strip().lower(), params


@dataclass(frozen=True, slots=True)
class UploadRequest:
    operation: UploadOperation
    boundary: bytes
    content_length: int | None


def validate_upload_headers(scope: Scope, limits: UploadLimits) -> UploadRequest:
    """Cheap upload preflight AFTER auth, usable BEFORE lease reservation."""
    operation = operation_for_scope(scope)
    if operation is None:
        raise UploadError(404, "upload_route_error", "Diese Uploadoperation ist nicht verfügbar.")
    values = dict(validate_headers(scope, limits))
    encoding = values.get(b"content-encoding", b"identity").strip().lower()
    if encoding != b"identity":
        raise UploadError(415, "upload_media_type_error", "Zusätzliche Uploadkodierungen werden nicht unterstützt.")
    value = values.get(b"content-type", b"")
    if len(value) > limits.max_content_type_bytes:
        raise UploadError(431, "request_header_limit_error", "Die HTTP-Kopfzeilen überschreiten die zulässige Größe.")
    media_type, params = _parameters(value)
    if media_type != b"multipart/form-data":
        raise UploadError(415, "upload_media_type_error", "Die Datei muss als multipart/form-data übertragen werden.")
    if set(params) - {b"boundary", b"charset"} or params.get(b"charset", b"utf-8").lower() != b"utf-8":
        raise _protocol()
    boundary = params.get(b"boundary", b"")
    if not _BOUNDARY.fullmatch(boundary) or boundary.endswith(b" "):
        raise _protocol()
    length = int(values[b"content-length"]) if b"content-length" in values else None
    if length is not None and length > limits.max_body_bytes:
        raise _too_large()
    return UploadRequest(operation, boundary, length)


class _MultipartUpload:
    def __init__(self, request: UploadRequest, limits: UploadLimits) -> None:
        self.request = request
        self.limits = limits
        self.buffer: bytearray | None = None
        self.size = 0
        self.filename = ""
        self.media_type: str | None = None
        self.values: dict[str, str] = {}
        self.seen: set[str] = set()
        self.complete = False
        self.part_open = False
        self.file_finished = False
        self.current_name = ""
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.header_bytes = 0
        self.headers: dict[bytes, bytes] = {}
        self.field_data = bytearray()
        self.fed_bytes = 0
        self.parser = MultipartParser(
            request.boundary,
            {
                "on_part_begin": self.part_begin,
                "on_header_field": self.header_field,
                "on_header_value": self.header_content,
                "on_header_end": self.header_end,
                "on_headers_finished": self.headers_finished,
                "on_part_data": self.part_data,
                "on_part_end": self.part_end,
                "on_end": self.end,
            },
            max_header_count=2,
            max_header_size=limits.max_part_header_bytes,
        )
        self.parser.logger = _PARSER_LOGGER

    def part_begin(self) -> None:
        if self.complete or self.part_open or len(self.seen) >= 1 + len(self.request.operation.fields):
            raise _protocol()
        self.part_open = True
        self.current_name = ""
        self.headers = {}
        self.header_bytes = 0
        self.field_data = bytearray()

    def _header_fragment(self, target: bytearray, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > self.limits.max_part_header_bytes:
            raise _protocol()
        target.extend(memoryview(data)[start:end])

    def header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_fragment(self.header_name, data, start, end)

    def header_content(self, data: bytes, start: int, end: int) -> None:
        self._header_fragment(self.header_value, data, start, end)

    def header_end(self) -> None:
        self.header_bytes += 4  # colon, space, CRLF
        name, value = bytes(self.header_name).lower(), bytes(self.header_value)
        if (
            self.header_bytes > self.limits.max_part_header_bytes
            or name not in {b"content-disposition", b"content-type"}
            or name in self.headers
            or _has_controls(value, allow_tab=True)
        ):
            raise _protocol()
        self.headers[name] = value
        self.header_name.clear()
        self.header_value.clear()

    def headers_finished(self) -> None:
        disposition, params = _parameters(self.headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or set(params) - {b"name", b"filename"}:
            raise _protocol()
        name = params.get(b"name", b"")
        allowed = {b"file", *(field.encode("ascii") for field in self.request.operation.fields)}
        if name not in allowed:
            raise _protocol()
        self.current_name = name.decode("ascii")
        if self.current_name in self.seen:
            raise _protocol()
        self.seen.add(self.current_name)
        media = self.headers.get(b"content-type")
        if media is not None:
            media_base, media_params = _parameters(media)
            if not _MEDIA_TYPE.fullmatch(media_base) or media_base.startswith(b"multipart/"):
                raise _protocol()
            if self.current_name != "file" and (
                media_base != b"text/plain"
                or set(media_params) - {b"charset"}
                or media_params.get(b"charset", b"utf-8").lower() != b"utf-8"
            ):
                raise _protocol()
        if self.current_name == "file":
            filename = params.get(b"filename")
            if not filename:
                raise _validation("file", "missing", "Ein Dateifeld mit Dateiname ist erforderlich.")
            if len(filename) > self.limits.max_filename_bytes or _has_controls(filename):
                raise _protocol()
            try:
                self.filename = filename.decode("utf-8")
            except UnicodeDecodeError:
                self.filename = filename.decode("latin-1")
            self.media_type = media.decode("latin-1") if media is not None else None
            self.buffer = bytearray(self.limits.max_upload_bytes)
        elif b"filename" in params:
            raise _protocol()

    def part_data(self, data: bytes, start: int, end: int) -> None:
        if not self.part_open or not self.current_name:
            raise _protocol()
        view = memoryview(data)[start:end]
        if self.current_name == "file":
            if self.size + len(view) > self.limits.max_upload_bytes:
                raise _too_large()
            assert self.buffer is not None
            self.buffer[self.size : self.size + len(view)] = view
            self.size += len(view)
        else:
            if len(self.field_data) + len(view) > self.limits.max_field_bytes:
                raise _too_large()
            self.field_data.extend(view)

    def part_end(self) -> None:
        if not self.part_open or not self.current_name:
            raise _protocol()
        if self.current_name == "file":
            self.file_finished = True
        else:
            try:
                self.values[self.current_name] = self.field_data.decode("ascii")
            except UnicodeDecodeError:
                raise _validation(
                    self.current_name, "string_type", "Das Steuerfeld muss ASCII-Text enthalten."
                ) from None
        self.part_open = False

    def end(self) -> None:
        if self.part_open or self.complete:
            raise _protocol()
        self.complete = True

    def feed(self, data: bytes) -> None:
        if self.parser.write(data) != len(data):
            raise _protocol()
        self.fed_bytes += len(data)
        # The parser may still hold a possible boundary prefix that later turns
        # out to be file bytes. Final overhead below is checked without slack.
        if self.fed_bytes - self.size > self.limits.max_overhead_bytes + len(self.request.boundary) + 8:
            raise _too_large()

    def finish(self, total: int) -> ReceivedUpload:
        self.parser.finalize()
        if not self.complete or self.part_open:
            raise _protocol()
        if self.buffer is None or not self.file_finished:
            raise _validation("file", "missing", "Das Dateifeld ist erforderlich.")
        if total - self.size > self.limits.max_overhead_bytes:
            raise _too_large()
        official = self.values.get("official", "")
        bool_values = {
            "1": True,
            "true": True,
            "t": True,
            "on": True,
            "yes": True,
            "y": True,
            "0": False,
            "false": False,
            "f": False,
            "off": False,
            "no": False,
            "n": False,
        }
        if official and official.lower() not in bool_values:
            raise _validation("official", "bool_parsing", "Der Wert für die offizielle Prüfung ist ungültig.")
        report_scope = self.values.get("scope", "") or "readable"
        if report_scope not in {"readable", "complete"}:
            raise _validation("scope", "literal_error", "Der Berichtsumfang muss readable oder complete sein.")
        options = UploadOptions(
            bool_values[official.lower()] if official else True,
            "complete" if report_scope == "complete" else "readable",
        )
        return ReceivedUpload(
            self.request.operation.operation,
            self.filename,
            self.media_type,
            options,
            memoryview(self.buffer)[: self.size].toreadonly(),
        )


async def receive_upload(scope: Scope, receive: Receive, limits: UploadLimits) -> ReceivedUpload:
    """Receive one admitted upload; propagate cancellation and disconnect.

    The returned exact-length view owns the sole upload buffer. Call close()
    after IPC consumption, including every error/cancellation path.
    """
    request = validate_upload_headers(scope, limits)
    start = last_data = monotonic()
    state = _MultipartUpload(request, limits)
    total = 0

    def remaining() -> float:
        now = monotonic()
        result = min(start + limits.total_timeout_seconds - now, last_data + limits.idle_timeout_seconds - now)
        if result <= 0:
            raise _timeout()
        return result

    try:
        while True:
            async with asyncio.timeout(remaining()):
                message = await receive()
            remaining()
            if message["type"] == "http.disconnect":
                raise ClientDisconnect()
            if message["type"] != "http.request":
                raise _protocol()
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise _protocol()
            total += len(body)
            if total > limits.max_body_bytes:
                raise _too_large()
            if request.content_length is not None and total > request.content_length:
                raise _protocol()
            if body:
                last_data = monotonic()
            for offset in range(0, len(body), limits.feed_bytes):
                remaining()
                state.feed(body[offset : offset + limits.feed_bytes])
                remaining()
                await asyncio.sleep(0)
            if not message.get("more_body", False):
                if request.content_length is not None and request.content_length != total:
                    raise _protocol()
                return state.finish(total)
            await asyncio.sleep(0)
    except TimeoutError:
        raise _timeout() from None
    except (FormParserError, MultipartParseError):
        raise _protocol() from None
    except (MemoryError, OverflowError):
        raise UploadError(
            503, "processing_unavailable_error", "Der Uploadspeicher ist derzeit nicht verfügbar."
        ) from None
    finally:
        # Parser callbacks form a cycle: explicitly remove its buffer ownership
        # rather than relying on delayed cyclic GC for 25 MiB allocations.
        state.buffer = None


def upload_openapi(path: str, limits: UploadLimits) -> dict[str, Any]:
    """Fresh route metadata; no automatic FastAPI body parsing is required."""
    operation = UPLOAD_OPERATIONS[path]
    properties: dict[str, Any] = {
        "file": {
            "type": "string",
            "format": "binary",
            "description": f"Genau eine Datei mit höchstens {limits.max_upload_bytes} Byte.",
        }
    }
    if "official" in operation.fields:
        properties["official"] = {"type": "boolean", "default": True}
    if "scope" in operation.fields:
        properties["scope"] = {"type": "string", "enum": ["readable", "complete"], "default": "readable"}
    descriptions = {
        400: "Nicht eindeutige oder beschädigte Uploadstruktur.",
        408: "Uploadzeit überschritten.",
        413: "Datei- oder Requestgrenze überschritten.",
        415: "Nicht unterstützter Uploadmedientyp.",
        422: "Ungültige Formularfelder oder Rechnung.",
        431: "HTTP-Kopfzeilen zu groß.",
        503: "Begrenzte Verarbeitungskapazität nicht verfügbar.",
    }
    return {
        "requestBody": {
            "required": True,
            "description": f"Multipart-Body höchstens {limits.max_body_bytes} Byte; Nicht-Dateianteil höchstens {limits.max_overhead_bytes} Byte.",
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "additionalProperties": False,
                        "properties": properties,
                    }
                }
            },
        },
        "responses": {status: {"description": detail} for status, detail in descriptions.items()},
    }
