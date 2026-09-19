from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.configuration import Settings
from app.desktop_security import DesktopSessionMiddleware
from app.http_upload import HeaderValidationMiddleware, SecurityHeadersMiddleware, UploadProcessingMiddleware
from app.processing.budgets import ProcessingError


def request_scope(path="/api/xml", *, root_path="", headers=None):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": root_path + path,
        "root_path": root_path,
        "query_string": b"",
        "server": ("localhost", 8000),
        "client": ("127.0.0.1", 1234),
        "headers": headers if headers is not None else [(b"content-type", b"multipart/form-data; boundary=test")],
    }


BODY = b'--test\r\nContent-Disposition: form-data; name="file"; filename="test.xml"\r\n\r\n<test/>\r\n--test--\r\n'


class Lease:
    def __init__(self, *, error=None, block=False):
        self.error = error
        self.block = block
        self.started = asyncio.Event()
        self.cleaned = False
        self.released = False
        self.upload = None
        self.result = SimpleNamespace(chunks=[b"<test", b"/>"], body_size=7, media_type="application/xml", headers={})

    async def run(self, upload, app_settings):
        self.upload = upload
        assert bytes(upload.payload) == b"<test/>"
        self.started.set()
        try:
            if self.block:
                await asyncio.Event().wait()
            if self.error:
                raise self.error
            return self.result
        finally:
            self.cleaned = True

    def release(self):
        assert self.cleaned or self.upload is None
        self.released = True


class Manager:
    def __init__(self, lease=None, *, available=True, send_seconds=1):
        self.lease = lease if lease is not None else Lease()
        self.available = available
        self.acquisitions = 0
        self.budgets = SimpleNamespace(send_seconds=send_seconds)

    def try_acquire(self):
        self.acquisitions += 1
        return self.lease if self.available else None


async def unreachable(scope, receive, send):
    raise AssertionError("Upload must not reach the automatic route parser")


def middleware(manager, *, settings=None, auth=False):
    settings = settings or Settings()
    app = UploadProcessingMiddleware(unreachable, get_manager=lambda: manager, get_settings=lambda: settings)
    if auth:
        app = DesktopSessionMiddleware(app, token=None, port=None, api_token="a" * 32)
    app = HeaderValidationMiddleware(app)
    return SecurityHeadersMiddleware(app)


async def invoke(app, *, scope=None, body=BODY, after_body=None, send_hook=None):
    messages = []
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        if after_body:
            return await after_body()
        await asyncio.Event().wait()

    async def send(message):
        if send_hook:
            await send_hook(message)
        messages.append(message)

    await app(scope or request_scope(), receive, send)
    return messages, calls


def test_capacity_precedes_first_receive_and_applies_to_xml():
    manager = Manager(available=False)
    messages, calls = asyncio.run(invoke(middleware(manager)))
    assert calls == 0
    assert manager.acquisitions == 1
    assert messages[0]["status"] == 503
    assert dict(messages[0]["headers"])[b"retry-after"] == b"65"
    assert json.loads(messages[1]["body"])["type"] == "analysis_capacity_error"


@pytest.mark.parametrize("path", ["/api/analyze", "/api/xml", "/api/report", "/api/report/pdf"])
@pytest.mark.parametrize("extra", [0, 1])
def test_every_upload_route_enforces_the_real_25mib_boundary_before_work(path, extra):
    limit = 25 * 1024**2

    class BoundaryLease(Lease):
        async def run(self, upload, app_settings):
            self.upload = upload
            assert upload.size == limit and upload.payload.readonly
            self.cleaned = True
            return self.result

    async def scenario():
        lease = BoundaryLease()
        owner = Manager(lease)
        responses = []
        prefix, suffix = BODY.split(b"<test/>")

        async def chunks():
            yield prefix
            remaining = limit + extra
            while remaining:
                size = min(65536, remaining)
                yield b"x" * size
                remaining -= size
            yield suffix

        source = chunks()

        async def receive():
            try:
                data = await anext(source)
            except StopAsyncIteration:
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": data, "more_body": True}

        # The disconnect observer starts only after the complete request.
        ended = False

        async def bounded_receive():
            nonlocal ended
            if ended:
                await asyncio.Event().wait()
            message = await receive()
            ended = not message["more_body"]
            return message

        async def send(message):
            responses.append(message)

        await middleware(owner)(request_scope(path), bounded_receive, send)
        assert responses[0]["status"] == (413 if extra else 200)
        assert lease.released and owner.acquisitions == 1
        if extra:
            assert lease.upload is None and not lease.cleaned
            assert json.loads(responses[1]["body"])["type"] == "upload_limit_error"
        else:
            assert lease.upload is not None
            with pytest.raises(ValueError):
                bytes(lease.upload.payload)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "headers,status",
    [
        ([(b"authorization", b"Bearer " + b"a" * 32), (b"authorization", b"Bearer " + b"a" * 32)], 400),
        ([(b"x-large", b"a" * 9000)], 431),
        ([(b"content-type", b"multipart/form-data; boundary=test")], 403),
    ],
)
def test_header_validation_precedes_auth_and_admission(headers, status):
    manager = Manager()
    messages, calls = asyncio.run(invoke(middleware(manager, auth=True), scope=request_scope(headers=headers)))
    assert calls == manager.acquisitions == 0
    assert messages[0]["status"] == status
    assert dict(messages[0]["headers"])[b"cache-control"] == b"no-store"
    assert dict(messages[0]["headers"])[b"x-content-type-options"] == b"nosniff"


def test_success_retains_lease_until_last_send_and_releases_upload_view():
    async def scenario():
        manager = Manager()

        async def send_hook(message):
            assert manager.lease.cleaned
            assert not manager.lease.released

        messages, _ = await invoke(middleware(manager), send_hook=send_hook, scope=request_scope(root_path="/mounted"))
        assert manager.lease.released
        with pytest.raises(ValueError):
            bytes(manager.lease.upload.payload)
        assert messages[0]["status"] == 200
        assert dict(messages[0]["headers"])[b"content-length"] == b"7"
        assert b"".join(item.get("body", b"") for item in messages) == b"<test/>"
        assert messages[-1]["more_body"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("during", ["run", "send"])
def test_disconnect_cancels_active_work_and_never_sends_second_response(during):
    async def scenario():
        lease = Lease(block=during == "run")
        manager = Manager(lease)
        sending = asyncio.Event()

        async def disconnected():
            await (lease.started if during == "run" else sending).wait()
            return {"type": "http.disconnect"}

        async def send_hook(message):
            if message["type"] == "http.response.body":
                sending.set()
                await asyncio.Event().wait()

        messages, _ = await invoke(middleware(manager), after_body=disconnected, send_hook=send_hook)
        assert lease.cleaned and lease.released
        assert len([item for item in messages if item["type"] == "http.response.start"]) == (during == "send")

    asyncio.run(scenario())


def test_external_cancellation_waits_for_run_cleanup_before_release():
    async def scenario():
        lease = Lease(block=True)
        task = asyncio.create_task(invoke(middleware(Manager(lease))))
        await lease.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert lease.cleaned and lease.released

    asyncio.run(scenario())


def test_send_timeout_never_emits_a_second_start_and_releases_capacity():
    async def scenario():
        manager = Manager(send_seconds=0.02)

        async def block_body(message):
            if message["type"] == "http.response.body":
                await asyncio.Event().wait()

        messages, _ = await asyncio.wait_for(invoke(middleware(manager), send_hook=block_body), 1)
        assert [item["status"] for item in messages if item["type"] == "http.response.start"] == [200]
        assert manager.lease.released

    asyncio.run(scenario())


def test_processing_failure_is_bounded_json_before_any_success_start():
    manager = Manager(Lease(error=ProcessingError(504, "processing_timeout_error", "Zeit überschritten.")))
    messages, _ = asyncio.run(invoke(middleware(manager)))
    assert messages[0]["status"] == 504
    assert json.loads(messages[1]["body"]) == {"type": "processing_timeout_error", "detail": "Zeit überschritten."}
    assert manager.lease.released


def test_upload_error_releases_lease_without_launching_processing():
    manager = Manager()
    messages, _ = asyncio.run(invoke(middleware(manager, settings=replace(Settings(), max_upload_bytes=2))))
    assert messages[0]["status"] == 413
    assert manager.lease.upload is None and manager.lease.released


def test_invalid_content_type_rejected_without_capacity_or_body():
    manager = Manager()
    messages, calls = asyncio.run(invoke(middleware(manager), scope=request_scope(headers=[])))
    assert messages[0]["status"] == 415
    assert manager.acquisitions == calls == 0


def test_non_upload_passes_without_acquiring_or_consuming_body():
    async def app(scope, receive, send):
        assert scope["path"] == "/api/health"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    manager = Manager()
    wrapped = UploadProcessingMiddleware(app, get_manager=lambda: manager, get_settings=Settings)
    messages, calls = asyncio.run(invoke(wrapped, scope=request_scope("/api/health")))
    assert calls == manager.acquisitions == 0
    assert messages[0]["status"] == 200


def test_api_upload_routes_have_no_automatic_multipart_dependencies():
    from app.main import app
    from app.upload_ingress import UPLOAD_OPERATIONS

    routes = {route.path: route for route in app.routes if getattr(route, "path", "") in UPLOAD_OPERATIONS}
    assert set(routes) == set(UPLOAD_OPERATIONS)
    for route in routes.values():
        assert not route.dependant.body_params
        assert not route.dependant.dependencies


def test_main_uses_only_asgi_middleware_and_preserves_openapi_contract():
    from app.main import app
    from app.upload_ingress import UPLOAD_OPERATIONS

    assert [item.cls.__name__ for item in app.user_middleware] == [
        "SecurityHeadersMiddleware",
        "HeaderValidationMiddleware",
        "DesktopSessionMiddleware",
        "UploadProcessingMiddleware",
    ]
    document = app.openapi()
    for path in UPLOAD_OPERATIONS:
        post = document["paths"][path]["post"]
        schema = post["requestBody"]["content"]["multipart/form-data"]["schema"]
        assert schema["required"] == ["file"]
        assert schema["properties"]["file"]["format"] == "binary"
        assert set(post["responses"]) >= {"200", "400", "408", "413", "415", "422", "431", "500", "503", "504"}
    assert document["paths"]["/api/analyze"]["post"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AnalysisResponse"
    }


@pytest.mark.parametrize(
    "path,operation",
    [
        ("/api/analyze", "analyze"),
        ("/api/xml", "export_xml"),
        ("/api/report", "report_html"),
        ("/api/report/pdf", "report_pdf"),
    ],
)
def test_main_dispatches_all_uploads_through_bound_operation(path, operation, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    manager = Manager()
    monkeypatch.setattr(main, "manager", manager)
    response = TestClient(main.app).post(path, files={"file": ("example.xml", b"<test/>", "application/xml")})
    assert response.status_code == 200
    assert manager.lease.upload.operation == operation
    assert manager.lease.released


def test_two_sending_reports_hold_both_slots_and_third_upload_is_not_received():
    async def scenario():
        leases = [Lease(), Lease()]
        started = 0
        both_sending = asyncio.Event()
        finish = asyncio.Event()

        class LimitedManager:
            budgets = SimpleNamespace(send_seconds=1)

            def try_acquire(self):
                for lease in leases:
                    if not getattr(lease, "acquired", False):
                        lease.acquired = True
                        return lease
                return None

        app = middleware(LimitedManager())

        async def stalled_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started += 1
                if started == 2:
                    both_sending.set()
                await finish.wait()

        first = asyncio.create_task(invoke(app, scope=request_scope("/api/report/pdf"), send_hook=stalled_send))
        second = asyncio.create_task(invoke(app, scope=request_scope("/api/report"), send_hook=stalled_send))
        try:
            await asyncio.wait_for(both_sending.wait(), 1)
            messages, calls = await invoke(app)
            assert calls == 0 and messages[0]["status"] == 503
            assert all(lease.cleaned and not lease.released for lease in leases)
        finally:
            finish.set()
            await asyncio.gather(first, second)
        assert all(lease.released for lease in leases)

    asyncio.run(scenario())


def test_slash_redirect_does_not_receive_or_allocate_before_canonical_route(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    manager = Manager()
    monkeypatch.setattr(main, "manager", manager)
    client = TestClient(main.app)
    response = client.post("/api/xml/", files={"file": ("x.xml", b"<test/>")}, follow_redirects=False)
    assert response.status_code == 307
    assert manager.acquisitions == 0
    assert response.headers["cache-control"] == "no-store"
    response = client.post("/api/xml/", files={"file": ("x.xml", b"<test/>")})
    assert response.status_code == 200
    assert manager.acquisitions == 1


def test_lifespan_opens_then_closes_admission_even_when_server_raises(monkeypatch):
    from app import main

    calls = []
    monkeypatch.setattr(
        main, "manager", SimpleNamespace(startup=lambda: calls.append("start"), shutdown=lambda: calls.append("stop"))
    )

    async def scenario():
        with pytest.raises(RuntimeError):
            async with main.lifespan(main.app):
                assert calls == ["start"]
                raise RuntimeError("Synthetic server failure")

    asyncio.run(scenario())
    assert calls == ["start", "stop"]


@pytest.mark.parametrize("limit", [0, -1, True, 25 * 1024**2 + 1, 2**1000])
def test_invalid_upload_configuration_never_allocates_or_receives(limit):
    manager = Manager()
    messages, calls = asyncio.run(invoke(middleware(manager, settings=replace(Settings(), max_upload_bytes=limit))))
    assert calls == manager.acquisitions == 0
    assert messages[0]["status"] == 503
    assert json.loads(messages[1]["body"])["type"] == "processing_unavailable_error"


@pytest.mark.parametrize("deadline", [0, -1, True, float("inf"), float("nan"), 361])
def test_invalid_response_deadline_never_reserves_or_receives(deadline):
    manager = Manager(send_seconds=deadline)
    messages, calls = asyncio.run(invoke(middleware(manager)))
    assert calls == manager.acquisitions == 0
    assert messages[0]["status"] == 503


def test_invalid_worker_snapshot_is_rejected_before_upload():
    manager = Manager()
    settings = replace(Settings(), kosit_timeout_seconds=float("inf"))
    messages, calls = asyncio.run(invoke(middleware(manager, settings=settings)))
    assert calls == manager.acquisitions == 0
    assert messages[0]["status"] == 503


def test_security_cache_contract_uses_outer_path_before_mount_changes_scope():
    from app.ui_contract import UI_STATIC_PREFIX

    async def mounted(scope, receive, send):
        scope["root_path"] = "/mounted" + UI_STATIC_PREFIX
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = request_scope(UI_STATIC_PREFIX + "/app.js", root_path="/mounted")
    messages, _ = asyncio.run(invoke(SecurityHeadersMiddleware(mounted), scope=scope))
    assert dict(messages[0]["headers"])[b"cache-control"] == b"public, max-age=31536000, immutable"
