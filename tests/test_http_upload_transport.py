"""Deterministic transport outcomes and cleanup ownership, without a listener."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.processing.budgets import ProcessingError
from app.upload_ingress import UploadError
from tests.test_http_upload import BODY, invoke, request_scope
from tests.test_processing_observation_api import ObservedLease, Owner, application, headers


class LifecycleLease(ObservedLease):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.upload = None
        self.cleaned = False
        self.releases = 0

    async def run(self, upload, settings):
        self.upload = upload
        try:
            return await super().run(upload, settings)
        finally:
            self.cleaned = True

    def release(self):
        assert self.upload is None or self.cleaned
        self.releases += 1
        super().release()

    def assert_released(self):
        assert self.released and self.releases == 1
        if self.upload is not None:
            with pytest.raises(ValueError):
                bytes(self.upload.payload)


def owner_for(response):
    error = ProcessingError(422, "processing_limit_error", "Synthetische Grenze.") if response == "error" else None
    return Owner(lease=LifecycleLease(error=error))


async def invoke_observed(owner, **kwargs):
    return await invoke(application(owner), scope=request_scope(headers=headers()), **kwargs)


def assert_no_other_tasks():
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("response", ["success", "error", "upload-error"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("disconnect_too", [False, True])
def test_send_exception_preserves_outcome_and_single_diagnosis(response, error_type, disconnect_too):
    async def scenario():
        owner = owner_for(response)
        disconnected = asyncio.Event()
        starts = []

        async def receive_disconnect():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def fail_body(message):
            if message["type"] == "http.response.start":
                starts.append(message["status"])
            else:
                if disconnect_too:
                    disconnected.set()
                raise error_type("synthetic send failure")

        options = {"body": b"broken"} if response == "upload-error" else {}
        call = invoke_observed(owner, after_body=receive_disconnect, send_hook=fail_body, **options)
        if error_type is OSError:
            await call
        else:
            with pytest.raises(error_type, match="synthetic send failure"):
                await call
        assert len(starts) == 1
        expected = ["upload_failed"] if response == "upload-error" else []
        assert owner.lease.events == [*expected, "response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error"])
def test_known_disconnect_immediately_before_send_does_not_invoke_sender(response):
    async def scenario():
        owner = owner_for(response)
        disconnected = asyncio.Event()
        observe = owner.lease.observe

        def observe_and_disconnect(phase):
            observe(phase)
            if phase == "response_sending":
                disconnected.set()

        owner.lease.observe = observe_and_disconnect

        async def receive_disconnect():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def forbidden_send(message):
            raise AssertionError("A known disconnected request must not begin another send.")

        await invoke_observed(owner, after_body=receive_disconnect, send_hook=forbidden_send)
        assert owner.lease.events == ["response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error"])
@pytest.mark.parametrize("blocked_message", ["start", "body", "final"])
@pytest.mark.parametrize("cause", ["disconnect", "cancel", "timeout"])
def test_incomplete_send_waits_for_cleanup_before_release(response, blocked_message, cause):
    async def scenario():
        owner = owner_for(response)
        if cause == "timeout":
            owner.budgets.send_seconds = 0.02
        sending = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_allowed = asyncio.Event()
        send_cleaned = False
        messages = []

        async def receive_disconnect():
            await sending.wait()
            if cause != "disconnect":
                await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def block_send(message):
            nonlocal send_cleaned
            kind = (
                "start"
                if message["type"] == "http.response.start"
                else "body"
                if message.get("more_body", False)
                else "final"
            )
            messages.append(message)
            # A JSON error response has a single, final body.
            target = "final" if response == "error" and blocked_message == "body" else blocked_message
            if kind == target:
                sending.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_allowed.wait()
                    send_cleaned = True

        task = asyncio.create_task(invoke_observed(owner, after_body=receive_disconnect, send_hook=block_send))
        try:
            await asyncio.wait_for(sending.wait(), 1)
            if cause == "cancel":
                task.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 1)
            assert not owner.lease.released and not task.done()
        finally:
            cleanup_allowed.set()
        if cause == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert send_cleaned
        assert len([m for m in messages if m["type"] == "http.response.start"]) == 1
        assert owner.lease.events == ["response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


def test_send_deadline_is_shared_by_all_chunks():
    async def scenario():
        owner = owner_for("success")
        owner.budgets.send_seconds = 0.05
        original_run = owner.lease.run
        chunks_sent = 0

        async def many_chunks(upload, settings):
            await original_run(upload, settings)
            return SimpleNamespace(chunks=[b"x"] * 100, body_size=100, media_type="text/plain", headers={})

        owner.lease.run = many_chunks

        async def progressing_send(message):
            nonlocal chunks_sent
            if message.get("more_body"):
                await asyncio.sleep(0.005)
                chunks_sent += 1

        await asyncio.wait_for(invoke_observed(owner, send_hook=progressing_send), 2)
        assert 0 < chunks_sent < 100
        assert owner.lease.events == ["response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error", "upload-error"])
def test_outer_cancellation_at_final_send_is_not_hidden_by_completion(response):
    async def scenario():
        owner = owner_for(response)
        final_sent = False

        async def cancel_at_end(message):
            nonlocal final_sent
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                final_sent = True
                task.cancel()

        task = asyncio.create_task(
            invoke_observed(owner, body=b"broken" if response == "upload-error" else BODY, send_hook=cancel_at_end)
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        assert final_sent
        expected = ["upload_failed"] if response == "upload-error" else []
        assert owner.lease.events == [*expected, "response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("completion_too", [False, True])
def test_processing_disconnect_retains_priority_and_awaits_cleanup(completion_too):
    async def scenario():
        owner = owner_for("success")
        disconnected = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_allowed = asyncio.Event()
        original_run = owner.lease.run

        async def processing(upload, settings):
            owner.lease.upload = upload
            disconnected.set()
            if completion_too:
                return await original_run(upload, settings)
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_allowed.wait()
                owner.lease.cleaned = True

        owner.lease.run = processing

        async def receive_disconnect():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        task = asyncio.create_task(invoke_observed(owner, after_body=receive_disconnect))
        if not completion_too:
            try:
                await asyncio.wait_for(cleanup_started.wait(), 1)
                assert not owner.lease.released and not task.done()
            finally:
                cleanup_allowed.set()
        messages, _ = await task
        assert not messages
        assert owner.lease.events == ["transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error"])
@pytest.mark.parametrize("exception_class", [UploadError, ProcessingError])
def test_send_exception_cannot_enter_an_ingress_or_processing_error_response(response, exception_class):
    async def scenario():
        owner = owner_for(response)
        failure = exception_class(400, "synthetic_send_error", "Synthetischer Versandfehler.")
        starts = []

        async def fail_once(message):
            if message["type"] == "http.response.start":
                starts.append(message["status"])
            elif len(starts) == 1:
                raise failure

        with pytest.raises(exception_class) as caught:
            await invoke_observed(owner, send_hook=fail_once)
        assert caught.value is failure
        assert starts == [200 if response == "success" else 422]
        assert owner.lease.events == ["response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error", "upload-error"])
def test_cancellation_after_success_publication_still_closes_upload_and_releases_lease(response):
    async def scenario():
        owner = owner_for(response)
        original_observe = owner.lease.observe

        def cancel_after_publication(phase):
            original_observe(phase)
            if phase == "response_send_complete":
                task.cancel()

        owner.lease.observe = cancel_after_publication
        task = asyncio.create_task(invoke_observed(owner, body=b"broken" if response == "upload-error" else BODY))
        with pytest.raises(asyncio.CancelledError):
            await task
        prefix = ["upload_failed"] if response == "upload-error" else []
        assert owner.lease.events == [*prefix, "response_sending", "response_send_complete"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


def test_repeated_outer_cancellation_does_not_interrupt_send_cleanup():
    async def scenario():
        owner = owner_for("success")
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_allowed = asyncio.Event()
        cleaned = False

        async def blocking_send(message):
            nonlocal cleaned
            if message["type"] == "http.response.body":
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_allowed.wait()
                    cleaned = True

        task = asyncio.create_task(invoke_observed(owner, send_hook=blocking_send))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        try:
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done() and not owner.lease.released
        finally:
            cleanup_allowed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned
        assert owner.lease.events == ["response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())
