from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.configuration import Settings
from app.desktop_security import DESKTOP_COOKIE_NAME, DesktopSessionMiddleware
from app.http_upload import HeaderValidationMiddleware, SecurityHeadersMiddleware, UploadProcessingMiddleware
from app.processing.budgets import ProcessingError
from tests.test_http_upload import invoke, request_scope

HEADER = "x-einvoice-observation-id"
OBSERVATION_ID = "a" * 32
TOKEN = "s" * 43
PATH = "/api/processing-observation"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class ObservedLease:
    def __init__(self, *, error=None, observe_error=False):
        self.events = []
        self.error = error
        self.observe_error = observe_error
        self.released = False

    def observe(self, phase):
        self.events.append(phase)
        if self.observe_error:
            raise RuntimeError("synthetic observation failure must not affect the invoice")

    async def run(self, upload, settings):
        if self.error:
            raise self.error
        return SimpleNamespace(chunks=[b"<test/>"], body_size=7, media_type="application/xml", headers={})

    def release(self):
        self.released = True


class Owner:
    def __init__(self, *, lease=None, record=None, conflict=False, snapshot_error=False):
        self.lease = lease or ObservedLease()
        self.record = record
        self.conflict = conflict
        self.snapshot_error = snapshot_error
        self.acquisitions = []
        self.lookups = []
        self.observations = self
        self.budgets = SimpleNamespace(send_seconds=1)

    def try_acquire(self, **kwargs):
        self.acquisitions.append(kwargs)
        if self.conflict:
            from app.processing.observation import ObservationConflict

            raise ObservationConflict("synthetic collision")
        return self.lease

    def snapshot(self, identifier):
        self.lookups.append(identifier)
        if self.snapshot_error:
            raise RuntimeError("synthetic secret must not reach an HTTP response")
        return self.record


def application(owner, *, token=TOKEN, desktop_token=None):
    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)
    api.add_middleware(UploadProcessingMiddleware, get_manager=lambda: owner, get_settings=Settings)
    api.add_middleware(DesktopSessionMiddleware, api_token=token, token=desktop_token, port=8000)
    api.add_middleware(HeaderValidationMiddleware)
    api.add_middleware(SecurityHeadersMiddleware)
    return api


def client(monkeypatch, owner, **kwargs):
    monkeypatch.setattr(main, "manager", owner)
    return TestClient(application(owner, **kwargs), base_url="http://127.0.0.1:8000")


def headers(*extra):
    return [
        (b"host", b"127.0.0.1:8000"),
        (b"content-type", b"multipart/form-data; boundary=test"),
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (HEADER.encode(), OBSERVATION_ID.encode()),
        *extra,
    ]


@pytest.mark.parametrize(
    "path,operation",
    [
        ("/api/analyze", "analyze"),
        ("/api/xml", "export_xml"),
        ("/api/report", "report_html"),
        ("/api/report/pdf", "report_pdf"),
    ],
)
def test_opt_in_upload_binds_exact_operation_and_send_phases(path, operation):
    owner = Owner()
    messages, _ = asyncio.run(invoke(application(owner), scope=request_scope(path, headers=headers())))
    assert messages[0]["status"] == 200
    assert owner.acquisitions == [{"observation_id": OBSERVATION_ID, "operation": operation}]
    assert owner.lease.events == ["response_sending", "response_send_complete"]
    assert owner.lease.released


@pytest.mark.parametrize("path", [PATH, "/api/xml"])
@pytest.mark.parametrize(
    "configured,authorization,cookie",
    [
        (None, None, None),
        (None, f"Bearer {TOKEN}", None),
        (TOKEN, None, None),
        (TOKEN, "Bearer wrong", None),
        (TOKEN, None, "desktop-session"),
        (TOKEN, "Bearer wrong", "desktop-session"),
    ],
)
def test_observation_always_requires_real_bearer(monkeypatch, path, configured, authorization, cookie):
    owner = Owner(record={"available": True})
    api = client(monkeypatch, owner, token=configured, desktop_token="desktop-session" if cookie else None)
    request_headers = {HEADER: OBSERVATION_ID}
    if authorization:
        request_headers["authorization"] = authorization
    if cookie:
        api.cookies.set(DESKTOP_COOKIE_NAME, cookie)
    response = (
        api.get(path, headers=request_headers)
        if path == PATH
        else api.post(path, headers=request_headers, files={"file": ("example.xml", b"<test/>", "application/xml")})
    )
    assert response.status_code == 403
    assert owner.acquisitions == owner.lookups == []


def test_dormant_middleware_cannot_forward_a_forged_internal_auth_scope():
    from app.processing.observation import BEARER_SCOPE_KEY

    owner = Owner()
    scope = request_scope(headers=[item for item in headers() if item[0] != b"authorization"])
    scope[BEARER_SCOPE_KEY] = True
    messages, calls = asyncio.run(invoke(application(owner, token=None), scope=scope))
    assert messages[0]["status"] == 403
    assert calls == 0
    assert owner.acquisitions == []


@pytest.mark.parametrize("path", [PATH, "/api/xml"])
@pytest.mark.parametrize(
    "value", ["", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "g", " " + "a" * 32, "a" * 32 + "," + "b" * 32, "é" * 32]
)
def test_noncanonical_ids_never_reach_ledger_or_upload(monkeypatch, path, value):
    owner = Owner(record={"available": True})
    api = client(monkeypatch, owner)
    extra = [(HEADER.encode(), value.encode("latin-1"))]
    response = (
        api.get(path, headers=[*AUTH.items(), *extra])
        if path == PATH
        else api.post(path, headers=[*AUTH.items(), *extra], files={"file": ("example.xml", b"<test/>")})
    )
    assert response.status_code == 400
    assert owner.acquisitions == owner.lookups == []


@pytest.mark.parametrize("path", [PATH, "/api/xml"])
def test_duplicate_ids_rejected_before_auth_and_body(monkeypatch, path):
    owner = Owner()
    scope = request_scope(path, headers=headers((b"X-Einvoice-Observation-Id", b"b" * 32)))
    if path == PATH:
        scope["method"] = "GET"
    messages, calls = asyncio.run(invoke(application(owner), scope=scope))
    assert messages[0]["status"] == 400
    assert calls == 0
    assert owner.acquisitions == owner.lookups == []


def test_collision_is_409_without_consuming_invoice():
    owner = Owner(conflict=True)
    messages, calls = asyncio.run(invoke(application(owner), scope=request_scope(headers=headers())))
    assert messages[0]["status"] == 409
    assert b"observation_conflict" in messages[1]["body"]
    assert calls == 0


@pytest.mark.parametrize(
    "record,status",
    [(None, 404), ({"available": False, "reason": "overflow"}, 200), ({"available": True, "job_id": "b" * 32}, 200)],
)
def test_lookup_is_exact_bounded_and_never_synthesizes_a_record(monkeypatch, record, status):
    owner = Owner(record=record)
    response = client(monkeypatch, owner).get(PATH, headers={**AUTH, HEADER: OBSERVATION_ID})
    assert response.status_code == status
    assert owner.lookups == [OBSERVATION_ID]
    assert owner.acquisitions == []
    assert response.headers["cache-control"] == "no-store"
    if record is not None:
        assert response.json() == record


@pytest.mark.parametrize(
    "suffix,extra",
    [("?pid=123", {}), ("?wait=1", {}), ("?", {"content-length": "1"}), ("", {"transfer-encoding": "chunked"})],
)
def test_status_rejects_query_and_body_control_channels(monkeypatch, suffix, extra):
    owner = Owner(record={"available": True})
    response = client(monkeypatch, owner).get(PATH + suffix, headers={**AUTH, HEADER: OBSERVATION_ID, **extra})
    assert response.status_code == 400
    assert owner.lookups == []


def test_status_requires_id_and_has_no_mutating_method(monkeypatch):
    owner = Owner(record={"available": True})
    api = client(monkeypatch, owner)
    assert api.get(PATH, headers=AUTH).status_code == 400
    assert api.post(PATH, headers={**AUTH, HEADER: OBSERVATION_ID}).status_code == 405
    assert owner.lookups == owner.acquisitions == []


@pytest.mark.parametrize("bad_snapshot", ["raises", "oversized"])
def test_status_snapshot_failure_has_only_fixed_unavailable_error(monkeypatch, bad_snapshot):
    owner = Owner(snapshot_error=bad_snapshot == "raises", record={"unexpected": "s" * (16 * 1024)})
    response = client(monkeypatch, owner).get(PATH, headers={**AUTH, HEADER: OBSERVATION_ID})
    assert response.status_code == 503
    assert response.json()["type"] == "observation_unavailable"
    assert "synthetic secret" not in response.text
    assert len(response.content) < 1024


def test_processing_error_has_a_completed_error_response_phase():
    owner = Owner(lease=ObservedLease(error=ProcessingError(422, "processing_limit_error", "Begrenzt.")))
    messages, _ = asyncio.run(invoke(application(owner), scope=request_scope(headers=headers())))
    assert messages[0]["status"] == 422
    assert owner.lease.events == ["response_sending", "response_send_complete"]


def test_upload_error_and_failed_response_send_do_not_claim_complete():
    owner = Owner()
    messages, _ = asyncio.run(invoke(application(owner), scope=request_scope(headers=headers()), body=b"broken"))
    assert messages[0]["status"] == 400
    assert owner.lease.events == ["upload_failed", "response_sending", "response_send_complete"]

    owner = Owner()

    async def failed_send(message):
        if message["type"] == "http.response.body":
            raise OSError("synthetic disconnected client")

    asyncio.run(invoke(application(owner), scope=request_scope(headers=headers()), send_hook=failed_send))
    assert owner.lease.events == ["response_sending", "transport_failed"]
    assert owner.lease.released


def test_observer_publication_failure_does_not_change_invoice_response():
    owner = Owner(lease=ObservedLease(observe_error=True))
    messages, _ = asyncio.run(invoke(application(owner), scope=request_scope(headers=headers())))
    assert messages[0]["status"] == 200
    assert owner.lease.released


@pytest.mark.parametrize("malformed_upload", [False, True])
def test_cancelled_response_send_never_claims_complete_and_releases(malformed_upload):
    owner = Owner()

    async def cancel_send(message):
        assert "response_send_complete" not in owner.lease.events
        if message["type"] == "http.response.body":
            raise asyncio.CancelledError

    options = {"body": b"broken"} if malformed_upload else {}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            invoke(application(owner), scope=request_scope(headers=headers()), send_hook=cancel_send, **options)
        )
    assert "response_send_complete" not in owner.lease.events
    assert owner.lease.events[-1] == "transport_failed"
    assert owner.lease.released


def test_observation_header_limit_is_rejected_without_receiving():
    owner = Owner()
    request_headers = [item for item in headers() if item[0] != HEADER.encode()]
    request_headers.append((HEADER.encode(), b"a" * 8193))
    messages, calls = asyncio.run(invoke(application(owner), scope=request_scope(headers=request_headers)))
    assert messages[0]["status"] == 431
    assert calls == 0
    assert owner.acquisitions == []


def test_mounted_upload_still_binds_observation_without_changing_caller_scope():
    owner = Owner()
    scope = request_scope(root_path="/mounted", headers=headers())
    messages, _ = asyncio.run(invoke(application(owner), scope=scope))
    assert messages[0]["status"] == 200
    assert owner.acquisitions == [{"observation_id": OBSERVATION_ID, "operation": "export_xml"}]
    from app.processing.observation import BEARER_SCOPE_KEY

    assert BEARER_SCOPE_KEY not in scope


def test_openapi_documents_observation_as_opt_in_header_not_body():
    document = main.app.openapi()
    for path, method, required in [
        (PATH, "get", True),
        ("/api/xml", "post", False),
        ("/api/analyze", "post", False),
        ("/api/report", "post", False),
        ("/api/report/pdf", "post", False),
    ]:
        operation = document["paths"][path][method]
        parameter = next(item for item in operation["parameters"] if item["name"].lower() == HEADER)
        assert parameter["in"] == "header"
        assert parameter["required"] is required
        assert parameter["schema"]["pattern"] == "^[0-9a-f]{32}$"
        assert "403" in operation["responses"]
        if method == "post":
            assert "409" in operation["responses"]
        else:
            assert "requestBody" not in operation


def test_ordinary_upload_keeps_noargument_admission_and_public_health(monkeypatch):
    owner = Owner()
    api = client(monkeypatch, owner, token=None)
    response = api.post("/api/xml", files={"file": ("example.xml", b"<test/>")})
    assert response.status_code == 200
    assert owner.acquisitions == [{}]
    assert owner.lease.events == []
    health = api.get("/api/health")
    assert health.status_code == 200
    assert "observation" not in health.text
    assert owner.lookups == []


def test_conformance_authentication_contract(monkeypatch):
    """Fixed CI node: real ASGI auth/admission, synthetic input, no native child."""
    variants = (
        (TOKEN, AUTH, None, 200),
        (TOKEN, {}, None, 403),
        (TOKEN, {"authorization": "Bearer wrong"}, None, 403),
        (TOKEN, {}, "desktop-session", 403),
        (TOKEN, {"authorization": "Bearer desktop-session"}, None, 403),
        (None, AUTH, None, 403),
        (None, {}, None, 403),
        (TOKEN, {**AUTH, "host": "example.invalid"}, None, 403),
        (TOKEN, {"origin": "http://example.invalid"}, "desktop-session", 403),
    )
    for configured, request_headers, cookie, status in variants:
        owner = Owner(record={"available": True, "observation_id": OBSERVATION_ID})
        api = client(monkeypatch, owner, token=configured, desktop_token="desktop-session")
        if cookie:
            api.cookies.set(DESKTOP_COOKIE_NAME, cookie)
        with api:
            response = api.post(
                "/api/xml",
                headers={**request_headers, HEADER: OBSERVATION_ID},
                files={"file": ("example.xml", b"<test/>", "application/xml")},
            )
            assert response.status_code == status
            snapshot = api.get(PATH, headers={**request_headers, HEADER: OBSERVATION_ID})
            assert snapshot.status_code == status
        if status == 200:
            assert response.content == b"<test/>"
            assert owner.acquisitions == [{"observation_id": OBSERVATION_ID, "operation": "export_xml"}]
            assert owner.lookups == [OBSERVATION_ID]
        else:
            assert owner.acquisitions == owner.lookups == []
