"""ASGI admission and transport; invoice parsing/rendering belongs to workers."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .configuration import Settings, settings_to_snapshot
from .processing.budgets import ProcessingError, unavailable
from .ui_contract import UI_STATIC_PREFIX
from .upload_ingress import (
    ReceivedUpload,
    UploadError,
    UploadLimits,
    operation_for_scope,
    receive_upload,
    validate_headers,
    validate_upload_headers,
)


class BufferedResult(Protocol):
    @property
    def chunks(self) -> list[bytes]: ...

    @property
    def body_size(self) -> int: ...

    @property
    def media_type(self) -> str: ...

    @property
    def headers(self) -> dict[str, str]: ...


class Lease(Protocol):
    async def run(self, upload: ReceivedUpload, app_settings: Settings) -> BufferedResult: ...

    def release(self) -> None: ...


class SendBudgets(Protocol):
    @property
    def send_seconds(self) -> float: ...


class Manager(Protocol):
    @property
    def budgets(self) -> SendBudgets: ...

    def try_acquire(self) -> Lease | None: ...


def _path(scope: Scope) -> str:
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :]
    return path


def _error_response(error: UploadError | ProcessingError) -> JSONResponse:
    return JSONResponse({"detail": error.detail, "type": error.error_type}, status_code=error.status)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # A mounted router can update root_path in-place before it sends.
        path = _path(scope)

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _value in headers}
                defaults = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                    b"content-security-policy": (
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                        b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                        b"base-uri 'self'; frame-ancestors 'none'"
                    ),
                }
                if path.startswith("/api/"):
                    defaults[b"cache-control"] = b"no-store"
                elif path.startswith(f"{UI_STATIC_PREFIX}/"):
                    defaults[b"cache-control"] = b"public, max-age=31536000, immutable"
                headers.extend((key, value) for key, value in defaults.items() if key not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secured_send)


class HeaderValidationMiddleware:
    """Normalize bounded API headers before auth constructs a Header/Cookie map."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _path(scope).startswith("/api/"):
            try:
                normalized = validate_headers(scope, UploadLimits())
            except UploadError as error:
                await _send_early(_error_response(error), scope, receive, send)
                return
            scope = {**scope, "headers": list(normalized)}
        await self.app(scope, receive, send)


async def _send_early(response: Response, scope: Scope, receive: Receive, send: Send) -> None:
    # Early rejection deliberately never calls receive (including Expect: 100-continue).
    try:
        await asyncio.wait_for(response(scope, receive, send), timeout=30.0)
    except (OSError, TimeoutError):
        return


async def _disconnected(receive: Receive) -> None:
    # The upload reader already consumed the complete body and relinquished receive.
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        # A second body or unknown event after EOF is not another upload.
        if message["type"] != "http.request" or message.get("body") or message.get("more_body", False):
            return
        await asyncio.sleep(0)


T = TypeVar("T")


async def _connected(awaitable: Awaitable[T], disconnect: asyncio.Task[None]) -> T:
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait((task, disconnect), return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done:
            raise ClientDisconnect
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        # Cancellation of run must finish its bounded native cleanup before release.
        await asyncio.gather(task, return_exceptions=True)


async def _send_result(result: BufferedResult, send: Send) -> None:
    headers = Response(media_type=result.media_type, headers=result.headers).raw_headers
    headers = [(key, value) for key, value in headers if key != b"content-length"]
    headers.append((b"content-length", str(result.body_size).encode("ascii")))
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    for chunk in result.chunks:
        await send({"type": "http.response.body", "body": chunk, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


class UploadProcessingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        get_manager: Callable[[], Manager],
        get_settings: Callable[[], Settings],
    ) -> None:
        self.app = app
        self.get_manager = get_manager
        self.get_settings = get_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if operation_for_scope(scope) is None:
            await self.app(scope, receive, send)
            return
        try:
            app_settings = self.get_settings()
            if type(app_settings.max_upload_bytes) is not int or not 0 < app_settings.max_upload_bytes <= 25 * 1024**2:
                raise ValueError("Upload configuration outside finite processing profile")
            settings_to_snapshot(app_settings)
            limits = UploadLimits(max_upload_bytes=app_settings.max_upload_bytes)
            manager = self.get_manager()
            send_seconds = manager.budgets.send_seconds
            if type(send_seconds) not in (int, float) or not math.isfinite(send_seconds) or not 0 < send_seconds <= 360:
                raise ValueError("Invalid response deadline")
        except (ValueError, TypeError, AttributeError):
            await _send_early(_error_response(unavailable()), scope, receive, send)
            return
        try:
            validate_upload_headers(scope, limits)
        except UploadError as error:
            await _send_early(_error_response(error), scope, receive, send)
            return
        lease = manager.try_acquire()
        if lease is None:
            await _send_early(
                JSONResponse(
                    {
                        "detail": "Der Prüfdienst ist ausgelastet. Bitte versuchen Sie es später erneut.",
                        "type": "analysis_capacity_error",
                    },
                    status_code=503,
                    headers={"Retry-After": str(min(max(app_settings.kosit_timeout_seconds + 5, 5), 600))},
                ),
                scope,
                receive,
                send,
            )
            return
        upload: ReceivedUpload | None = None
        disconnect: asyncio.Task[None] | None = None
        try:
            upload = await receive_upload(scope, receive, limits)
            disconnect = asyncio.create_task(_disconnected(receive))
            try:
                result = await _connected(lease.run(upload, app_settings), disconnect)
            except ProcessingError as error:
                await _connected(
                    asyncio.wait_for(_error_response(error)(scope, receive, send), send_seconds),
                    disconnect,
                )
                return
            await _connected(asyncio.wait_for(_send_result(result, send), send_seconds), disconnect)
        except UploadError as error:
            await _send_early(_error_response(error), scope, receive, send)
        except (ClientDisconnect, OSError, TimeoutError):
            # A started or disconnected response cannot receive another status line.
            return
        finally:
            if disconnect is not None:
                disconnect.cancel()
                await asyncio.gather(disconnect, return_exceptions=True)
            if upload is not None:
                upload.close()
            lease.release()
