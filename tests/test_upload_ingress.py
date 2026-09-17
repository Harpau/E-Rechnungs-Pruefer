from __future__ import annotations

import asyncio
import weakref
from dataclasses import FrozenInstanceError, replace

import pytest
from starlette.requests import ClientDisconnect

from app import upload_ingress as ingress


def multipart(parts, boundary=b"example-boundary"):
    result = bytearray()
    for headers, payload in parts:
        result.extend(b"--" + boundary + b"\r\n")
        for name, value in headers:
            result.extend(name + b": " + value + b"\r\n")
        result.extend(b"\r\n" + payload + b"\r\n")
    result.extend(b"--" + boundary + b"--\r\n")
    return bytes(result)


def file_part(payload=b"<example />", name=b"file", filename=b"example.xml"):
    return ([(b"Content-Disposition", b'form-data; name="' + name + b'"; filename="' + filename + b'"')], payload)


def field_part(name, value):
    return ([(b"Content-Disposition", b'form-data; name="' + name + b'"')], value)


def scope(path="/api/xml", headers=(), boundary=b"example-boundary", **extra):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "root_path": "",
        "http_version": "1.1",
        "headers": [(b"content-type", b'multipart/form-data; boundary="' + boundary + b'"'), *headers],
        **extra,
    }


def run_upload(body, *, request=None, limits=None, chunk_size=None):
    messages = [
        {"type": "http.request", "body": body[i : i + (chunk_size or len(body))], "more_body": True}
        for i in range(0, len(body), chunk_size or len(body))
    ]
    messages.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive():
        return messages.pop(0)

    return asyncio.run(
        ingress.receive_upload(request or scope(), receive, limits or ingress.UploadLimits(max_upload_bytes=1024))
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 17, 64, None])
def test_original_bytes_and_bounded_readonly_ownership(chunk_size):
    payload = b"\xff\xfe<\x00!\x00D\x00O\x00C\x00T\x00Y\x00P\x00E\x00 example>\r\n"
    result = run_upload(multipart([file_part(payload)]), chunk_size=chunk_size)
    assert result.operation == "export_xml"
    assert result.payload.readonly and bytes(result.payload) == payload
    assert result.payload.nbytes == result.size == len(payload)
    assert isinstance(result.payload.obj, bytearray) and len(result.payload.obj) == 1024
    with pytest.raises(TypeError):
        result.payload[0] = 0
    with pytest.raises(FrozenInstanceError):
        result.filename = "changed.xml"
    result.close()
    result.close()
    with pytest.raises(ValueError):
        bytes(result.payload)


@pytest.mark.parametrize(
    "path,fields",
    [
        ("/api/xml", []),
        ("/api/analyze", [b"official"]),
        ("/api/report", [b"official", b"scope"]),
        ("/api/report/pdf", [b"scope", b"official"]),
    ],
)
def test_all_operations_and_field_order(path, fields):
    parts = [field_part(f, b"false" if f == b"official" else b"complete") for f in fields]
    result = run_upload(multipart([*parts, file_part()]), request=scope(path))
    assert result.options.official == (b"official" not in fields)
    assert result.options.scope == ("complete" if b"scope" in fields else "readable")
    result.close()


@pytest.mark.parametrize(
    "value,expected",
    [
        (b"", True),
        (b"true", True),
        (b"ON", True),
        (b"1", True),
        (b"yes", True),
        (b"t", True),
        (b"Y", True),
        (b"false", False),
        (b"OFF", False),
        (b"0", False),
        (b"no", False),
        (b"F", False),
        (b"n", False),
    ],
)
def test_existing_boolean_forms(value, expected):
    result = run_upload(multipart([file_part(), field_part(b"official", value)]), request=scope("/api/analyze"))
    assert result.options.official is expected
    result.close()


def test_empty_optional_scope_keeps_default():
    result = run_upload(multipart([file_part(), field_part(b"scope", b"")]), request=scope("/api/report"))
    assert result.options.scope == "readable"
    result.close()


@pytest.mark.parametrize(
    "parts,path,status",
    [
        ([file_part(), file_part(b"")], "/api/xml", 400),
        ([file_part(), file_part(name=b"unused")], "/api/xml", 400),
        ([file_part(), field_part(b"official", b"true")], "/api/xml", 400),
        ([file_part(), field_part(b"other", b"x")], "/api/analyze", 400),
        ([file_part(), field_part(b"official", b"true"), field_part(b"official", b"true")], "/api/analyze", 400),
        ([field_part(b"file", b"not-a-file")], "/api/xml", 422),
        ([field_part(b"official", b"true")], "/api/analyze", 422),
        ([file_part(filename=b"")], "/api/xml", 422),
        ([file_part(), field_part(b"official", b"maybe")], "/api/analyze", 422),
        ([file_part(), field_part(b"scope", b"other")], "/api/report", 422),
    ],
)
def test_strict_parts_and_field_validation(parts, path, status):
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(multipart(parts), request=scope(path))
    assert caught.value.status == status
    if status == 422:
        assert isinstance(caught.value.detail, list)
        assert caught.value.detail[0]["loc"][0] == "body"


def test_file_and_true_overhead_limits_are_independent():
    limits = ingress.UploadLimits(max_upload_bytes=32, max_overhead_bytes=256)
    result = run_upload(multipart([file_part(b"x" * 32)]), limits=limits)
    result.close()
    with pytest.raises(ingress.UploadError, match="zulässige") as caught:
        run_upload(multipart([file_part(b"x" * 33)]), limits=limits)
    assert caught.value.status == 413
    small = multipart([file_part(b"x")])
    overhead = len(small) - 1
    result = run_upload(small, limits=replace(limits, max_overhead_bytes=overhead), chunk_size=1)
    result.close()
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(small + b"x", limits=replace(limits, max_overhead_bytes=overhead))
    assert caught.value.status == 413


@pytest.mark.parametrize(
    "headers",
    [
        [(b"Content-Disposition", b'form-data; name="file"; name="file"; filename="x.xml"')],
        [(b"Content-Disposition", b'form-data; name="file"; filename="a"; filename="b"')],
        [(b"Content-Disposition", b'form-data; name="file"; filename="a"; filename*=UTF-8\'\'b')],
        [(b"Content-Disposition", b'form-data; name="file"; filename="unterminated')],
        [
            (b"Content-Disposition", b'form-data; name="file"; filename="a"'),
            (b"Content-Disposition", b'form-data; name="file"'),
        ],
        [(b"Content-Disposition", b'form-data; name="file"; filename="a"'), (b"Content-Transfer-Encoding", b"base64")],
        [
            (b"Content-Disposition", b'form-data; name="file"; filename="a"'),
            (b"Content-Type", b"multipart/mixed; boundary=child"),
        ],
    ],
)
def test_ambiguous_part_metadata_never_selects_a_value(headers):
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(multipart([(headers, b"example")]))
    assert caught.value.status == 400


def test_unicode_filename_quoted_boundary_and_octet_stream():
    headers = [
        (b"Content-Disposition", 'form-data; name="file"; filename="Beispiel-ä.xml"'.encode()),
        (b"Content-Type", b"application/octet-stream"),
    ]
    result = run_upload(multipart([(headers, b"example")], b"a:b c"), request=scope(boundary=b"a:b c"))
    assert result.filename == "Beispiel-ä.xml" and result.media_type == "application/octet-stream"
    result.close()


@pytest.mark.parametrize("removed", [1, 4, 12, 24])
def test_missing_closing_boundary_is_not_finalize_success(removed):
    body = multipart([file_part()])
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(body[: -removed - 2])
    assert caught.value.status == 400


def test_complete_boundary_still_requires_http_eof():
    async def scenario():
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": multipart([file_part()]), "more_body": True}
            return {"type": "http.disconnect"}

        with pytest.raises(ClientDisconnect):
            await ingress.receive_upload(scope(), receive, ingress.UploadLimits(max_upload_bytes=1024))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"Content-Length", b"1")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"1, 1")],
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        [(b"authorization", b"Bearer example"), (b"Authorization", b"Bearer example")],
        [(b"cookie", b"einvoice_desktop_session=a; einvoice_desktop_session=b")],
        [(b"cookie", b"a=1"), (b"cookie", b"b=2")],
        [(b"x-example", b"first\r\nInjected: bad")],
    ],
)
def test_header_ambiguity_before_auth(headers):
    with pytest.raises(ingress.UploadError) as caught:
        ingress.validate_headers(scope(headers=headers), ingress.UploadLimits())
    assert caught.value.status == 400


def test_http2_cookies_normalized_without_changing_original_scope():
    request = scope(headers=[(b"cookie", b"a=1"), (b"cookie", b"einvoice_desktop_session=example")], http_version="2")
    normalized = ingress.validate_headers(request, ingress.UploadLimits())
    assert [v for k, v in normalized if k == b"cookie"] == [b"a=1; einvoice_desktop_session=example"]
    assert len([v for k, v in request["headers"] if k == b"cookie"]) == 2


@pytest.mark.parametrize(
    "http_scope,status",
    [
        (scope(headers=[(b"content-length", b"999999999")]), 413),
        (scope(headers=[(b"content-encoding", b"gzip")]), 415),
        (scope(headers=[(b"x-big", b"x" * 8193)]), 431),
        (scope(boundary=b"x" * 71), 400),
        (scope() | {"headers": [(b"content-type", b"application/xml")]}, 415),
    ],
)
def test_preflight_errors_do_not_receive(http_scope, status):
    async def receive():
        pytest.fail("preflight must not read body")

    with pytest.raises(ingress.UploadError) as caught:
        asyncio.run(ingress.receive_upload(http_scope, receive, ingress.UploadLimits()))
    assert caught.value.status == status


def test_content_length_must_match_stream():
    body = multipart([file_part()])
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(body, request=scope(headers=[(b"content-length", str(len(body) - 1).encode())]))
    assert caught.value.status == 400


def test_waiting_receive_is_timed_out():
    async def scenario():
        async def receive():
            await asyncio.sleep(10)

        with pytest.raises(ingress.UploadError) as caught:
            await ingress.receive_upload(scope(), receive, ingress.UploadLimits(idle_timeout_seconds=0.02))
        assert caught.value.status == 408

    asyncio.run(scenario())


def test_empty_events_do_not_extend_idle_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ingress, "monotonic", lambda: clock[0])

    async def receive():
        clock[0] += 1
        return {"type": "http.request", "body": b"", "more_body": True}

    with pytest.raises(ingress.UploadError) as caught:
        asyncio.run(ingress.receive_upload(scope(), receive, ingress.UploadLimits(idle_timeout_seconds=2)))
    assert caught.value.status == 408 and clock[0] <= 3


def test_continuous_real_bytes_do_not_reset_total_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ingress, "monotonic", lambda: clock[0])
    body = iter(multipart([file_part()]))

    async def receive():
        clock[0] += 0.2
        return {"type": "http.request", "body": bytes([next(body)]), "more_body": True}

    with pytest.raises(ingress.UploadError) as caught:
        asyncio.run(ingress.receive_upload(scope(), receive, ingress.UploadLimits(total_timeout_seconds=0.5)))
    assert caught.value.status == 408 and clock[0] < 1


@pytest.mark.parametrize("failure", [False, True])
def test_real_buffer_owner_released_without_waiting_for_cycle_gc(monkeypatch, failure):
    references = []

    class ObservedBuffer(bytearray):
        pass

    def allocate(value=0):
        buffer = ObservedBuffer(value)
        if value == 1024:
            references.append(weakref.ref(buffer))
        return buffer

    monkeypatch.setattr(ingress, "bytearray", allocate, raising=False)
    if failure:
        with pytest.raises(ingress.UploadError):
            run_upload(multipart([file_part(), file_part(b"")]))
    else:
        result = run_upload(multipart([file_part()]))
        assert references[0]() is not None
        result.close()
    assert len(references) == 1 and references[0]() is None


def test_upload_does_not_use_a_starlette_or_tempfile_spool(monkeypatch):
    import tempfile

    import starlette.formparsers

    def forbidden(*args, **kwargs):
        pytest.fail("No upload spooling is permitted")

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", forbidden)
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", forbidden)
    result = run_upload(multipart([file_part()]))
    result.close()


@pytest.mark.parametrize(
    "parts,limits",
    [
        ([file_part(filename=b"x" * 1025)], ingress.UploadLimits(max_upload_bytes=1024)),
        ([file_part(), field_part(b"official", b"x" * 65)], ingress.UploadLimits(max_upload_bytes=1024)),
        (
            [
                (
                    [
                        (b"Content-Disposition", b'form-data; name="file"; filename="x"'),
                        (b"Content-Type", b"text/plain"),
                    ],
                    b"example",
                )
            ],
            ingress.UploadLimits(max_upload_bytes=1024, max_part_header_bytes=64),
        ),
    ],
)
def test_part_metadata_budgets(parts, limits):
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(multipart(parts), request=scope("/api/analyze"), limits=limits, chunk_size=1)
    assert caught.value.status in {400, 413}


@pytest.mark.parametrize(
    "header",
    [
        b"multipart/form-data; boundary=example-boundary; boundary=example-boundary",
        b'multipart/form-data; boundary="example-boundary"; charset=iso-8859-1',
        b'multipart/form-data; boundary="example-boundary"; bad',
        b'multipart/form-data; boundary="example-boundary" garbage',
    ],
)
def test_bad_mime_parameters_fail_before_receive(header):
    async def receive():
        pytest.fail("No body read")

    with pytest.raises(ingress.UploadError) as caught:
        asyncio.run(
            ingress.receive_upload(scope() | {"headers": [(b"content-type", header)]}, receive, ingress.UploadLimits())
        )
    assert caught.value.status == 400


def test_chunked_missing_length_and_claimed_longer_length():
    body = multipart([file_part()])
    result = run_upload(body, request=scope(headers=[(b"transfer-encoding", b"chunked")]), chunk_size=3)
    result.close()
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(body, request=scope(headers=[(b"content-length", str(len(body) + 1).encode())]))
    assert caught.value.status == 400


def test_global_body_limit_is_checked_before_parser(monkeypatch):
    def forbidden(*args):
        pytest.fail("Oversized ASGI body must not reach parser")

    monkeypatch.setattr(ingress._MultipartUpload, "feed", forbidden)
    with pytest.raises(ingress.UploadError) as caught:
        run_upload(b"x" * 50, limits=ingress.UploadLimits(max_upload_bytes=10, max_overhead_bytes=20))
    assert caught.value.status == 413


def test_cancel_propagates_instead_of_becoming_http_success():
    async def receive():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingress.receive_upload(scope(), receive, ingress.UploadLimits()))


def test_route_mount_and_openapi_are_one_contract():
    request = scope(path="/prefix/api/report/pdf", root_path="/prefix")
    result = run_upload(multipart([file_part()]), request=request)
    assert result.operation == "report_pdf"
    result.close()
    for path, operation in ingress.UPLOAD_OPERATIONS.items():
        schema = ingress.upload_openapi(path, ingress.UploadLimits())
        form = schema["requestBody"]["content"]["multipart/form-data"]["schema"]
        assert form["required"] == ["file"] and form["additionalProperties"] is False
        assert set(form["properties"]) == {"file", *operation.fields}
        assert {400, 408, 413, 415, 431, 503}.issubset(schema["responses"])


@pytest.mark.parametrize(
    "changes",
    [
        {"max_upload_bytes": 0},
        {"max_upload_bytes": True},
        {"max_overhead_bytes": -1},
        {"idle_timeout_seconds": float("nan")},
        {"total_timeout_seconds": float("inf")},
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        ingress.UploadLimits(**changes)


def test_operation_names_match_worker_protocol():
    assert {path: spec.operation for path, spec in ingress.UPLOAD_OPERATIONS.items()} == {
        "/api/analyze": "analyze",
        "/api/xml": "export_xml",
        "/api/report": "report_html",
        "/api/report/pdf": "report_pdf",
    }


def test_joined_http2_cookie_obeys_normalized_header_budget():
    request = scope(headers=[(b"cookie", b"a=" + b"a" * 4094), (b"cookie", b"b=" + b"b" * 4094)], http_version="2")
    with pytest.raises(ingress.UploadError) as caught:
        ingress.validate_headers(request, ingress.UploadLimits())
    assert caught.value.status == 431


@pytest.mark.parametrize("whitespace", [b" ", b"\t", b"\xa0", b"\x85"])
@pytest.mark.parametrize("split", [False, True])
def test_duplicate_desktop_session_matches_starlette_cookie_key_normalization(whitespace, split):
    first = b"einvoice_desktop_session=first"
    second = b"einvoice_desktop_session" + whitespace + b"=second"
    headers = [(b"cookie", first), (b"cookie", second)] if split else [(b"cookie", first + b"; " + second)]
    request = scope(headers=headers, http_version="2" if split else "1.1")
    with pytest.raises(ingress.UploadError) as caught:
        ingress.validate_headers(request, ingress.UploadLimits())
    assert caught.value.status == 400
