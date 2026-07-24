from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.desktop_security import DESKTOP_COOKIE_NAME, DesktopSessionMiddleware, OneTimeBrowserSessions
from app.windows_service_ipc import (
    PIPE_SECURITY_SDDL,
    SERVICE_PIPE_NAME,
    decode_open_acknowledgement,
    decode_open_request,
    decode_open_response,
    encode_open_acknowledgement,
    encode_open_request,
    encode_open_response,
)


def _service_client(
    broker: OneTimeBrowserSessions,
    *,
    api_token: str = "a" * 43,
) -> TestClient:
    protected = FastAPI()

    @protected.get("/")
    async def index():
        return {"ok": True}

    @protected.get("/api/health")
    async def health():
        return {"status": "ok"}

    @protected.post("/api/action")
    async def action():
        return {"done": True}

    protected.add_middleware(
        DesktopSessionMiddleware,
        port=8080,
        api_token=api_token,
        browser_sessions=broker,
    )
    return TestClient(protected, base_url="http://127.0.0.1:8080")


def test_service_bootstrap_is_short_lived_single_use_and_not_the_bearer_token() -> None:
    now = [1000.0]
    tokens = iter(("bootstrap-token", "browser-session"))
    broker = OneTimeBrowserSessions(
        bootstrap_ttl_seconds=60,
        session_ttl_seconds=1800,
        clock=lambda: now[0],
        token_factory=lambda: next(tokens),
    )
    api_token = "a" * 43
    client = _service_client(broker, api_token=api_token)
    bootstrap = broker.issue_bootstrap()

    assert bootstrap != api_token
    first = client.get(f"/desktop/bootstrap?token={bootstrap}", follow_redirects=False)
    replay = client.get(f"/desktop/bootstrap?token={bootstrap}", follow_redirects=False)

    assert first.status_code == 303
    assert first.headers["location"] == "/"
    assert f"{DESKTOP_COOKIE_NAME}=browser-session" in first.headers["set-cookie"]
    assert api_token not in first.headers["set-cookie"]
    assert "HttpOnly" in first.headers["set-cookie"]
    assert "SameSite=strict" in first.headers["set-cookie"]
    assert replay.status_code == 403
    assert client.get("/").status_code == 200

    now[0] += 1801
    assert client.get("/").status_code == 403


def test_expired_service_bootstrap_is_rejected() -> None:
    now = [1000.0]
    broker = OneTimeBrowserSessions(clock=lambda: now[0])
    client = _service_client(broker)
    bootstrap = broker.issue_bootstrap()
    now[0] += 61

    assert client.get(f"/desktop/bootstrap?token={bootstrap}", follow_redirects=False).status_code == 403


def test_service_browser_session_tables_are_bounded() -> None:
    tokens = iter(("bootstrap-1", "bootstrap-2", "bootstrap-3", "session-1", "session-2", "bootstrap-4", "session-3"))
    broker = OneTimeBrowserSessions(
        max_pending_bootstraps=2,
        max_sessions=2,
        token_factory=lambda: next(tokens),
    )

    first = broker.issue_bootstrap()
    second = broker.issue_bootstrap()
    third = broker.issue_bootstrap()

    assert broker.consume_bootstrap(first) is None
    first_session = broker.consume_bootstrap(second)
    second_session = broker.consume_bootstrap(third)
    fourth = broker.issue_bootstrap()
    third_session = broker.consume_bootstrap(fourth)

    assert first_session is not None
    assert second_session is not None
    assert third_session is not None
    assert broker.session_is_valid(first_session) is False
    assert broker.session_is_valid(second_session) is True
    assert broker.session_is_valid(third_session) is True


def test_service_bearer_authorizes_api_but_never_browser_page() -> None:
    api_token = "a" * 43
    client = _service_client(OneTimeBrowserSessions(), api_token=api_token)

    assert client.post("/api/action").status_code == 403
    assert client.post("/api/action", headers={"authorization": "Bearer falsch"}).status_code == 403
    assert client.post("/api/action", headers={"authorization": f"Bearer {api_token}"}).status_code == 200
    assert client.get("/", headers={"authorization": f"Bearer {api_token}"}).status_code == 403


def test_named_pipe_protocol_accepts_only_the_open_command_and_never_contains_bearer() -> None:
    request = encode_open_request()
    response = encode_open_response("http://127.0.0.1:8080/desktop/bootstrap?token=kurzlebig")
    acknowledgement = encode_open_acknowledgement(response)

    assert decode_open_request(request) == "open"
    assert decode_open_response(response).endswith("token=kurzlebig")
    assert decode_open_acknowledgement(acknowledgement, response) is None
    assert json.loads(response)["version"] == 1
    assert json.loads(acknowledgement) == {
        "response_sha256": "d6c35ea728c9252a65a72d7e1a610007006830715575c4632f8174dbea67531a",
        "version": 1,
    }
    assert "Authorization" not in response.decode("utf-8")
    assert "kurzlebig" not in acknowledgement.decode("utf-8")
    assert SERVICE_PIPE_NAME.startswith("\\\\.\\pipe\\")
    assert ";;;IU)" in PIPE_SECURITY_SDDL

    with pytest.raises(ValueError, match="IPC-Befehl"):
        decode_open_request(b'{"version":1,"action":"rotate-token"}')
    with pytest.raises(ValueError, match="IPC-Nachricht"):
        decode_open_request(b"x" * 5000)
    with pytest.raises(ValueError, match="nicht gültig bestätigt"):
        decode_open_acknowledgement(
            b'{"response_sha256":"00","version":1}',
            response,
        )
