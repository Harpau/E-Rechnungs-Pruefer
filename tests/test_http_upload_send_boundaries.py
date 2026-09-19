"""Send completion at real deadlines and a modeled Python 3.11 scheduling gap."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_http_upload import BODY
from tests.test_http_upload_transport import assert_no_other_tasks, invoke_observed, owner_for


@pytest.mark.parametrize("response", ["success", "error", "upload-error"])
@pytest.mark.parametrize("outer_cancel", [False, True])
def test_deadline_expiring_before_completion_is_published_never_claims_success(monkeypatch, response, outer_cancel):
    async def scenario():
        owner = owner_for(response)
        original_timeout = asyncio.timeout
        send_timeouts = []
        seconds = 30.0 if response == "upload-error" else owner.budgets.send_seconds
        completed_final_body = False
        started_statuses = []

        def capture_timeout(delay):
            context = original_timeout(delay)
            if delay == seconds:
                send_timeouts.append(context)
            return context

        monkeypatch.setattr(asyncio, "timeout", capture_timeout)

        async def expire_at_final_body(message):
            nonlocal completed_final_body
            if message["type"] == "http.response.start":
                started_statuses.append(message["status"])
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                assert len(send_timeouts) == 1
                # Use the real timeout callback, scheduled before the suspended
                # caller can publish success. The final send itself then returns.
                loop = asyncio.get_running_loop()
                send_timeouts[0].reschedule(loop.time())
                if outer_cancel:
                    # Both callbacks run before the parent resumes; this extra
                    # cancellation must remain distinct from the timeout's own.
                    loop.call_at(loop.time(), task.cancel)
                completed_final_body = True

        task = asyncio.create_task(
            invoke_observed(
                owner,
                body=b"broken" if response == "upload-error" else BODY,
                send_hook=expire_at_final_body,
            )
        )
        if outer_cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert completed_final_body and send_timeouts[0].expired()
        assert started_statuses == [{"success": 200, "error": 422, "upload-error": 400}[response]]
        expected = ["upload_failed"] if response == "upload-error" else []
        assert owner.lease.events == [*expected, "response_sending", "transport_failed"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", ["success", "error"])
def test_actual_send_completion_wins_while_deadline_wrapper_would_still_be_pending(monkeypatch, response):
    async def scenario():
        owner = owner_for(response)
        disconnected = asyncio.Event()
        wrapper_resume = asyncio.Event()
        final_body_sent = False
        observe = owner.lease.observe

        def observe_outcome(phase):
            observe(phase)
            if phase in {"response_send_complete", "transport_failed"}:
                wrapper_resume.set()

        owner.lease.observe = observe_outcome

        async def separate_task_wait_for(awaitable, timeout):
            # Model Python 3.11's separate inner task and callback-mediated waiter.
            # Python 3.14's wait_for directly awaits its input for positive timeouts,
            # so ordinary RAM protocol tests there do not expose this extra hop.
            task = asyncio.create_task(awaitable)
            waiter = asyncio.get_running_loop().create_future()

            def wake_waiter(completed):
                if not waiter.done():
                    waiter.set_result(None)

            task.add_done_callback(wake_waiter)
            try:
                async with asyncio.timeout(timeout):
                    await waiter
                    # Hold this continuation to make inner-done/wrapper-pending
                    # deterministic, instead of relying on event-loop ordering.
                    await wrapper_resume.wait()
                    return task.result()
            finally:
                task.remove_done_callback(wake_waiter)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        monkeypatch.setattr(asyncio, "wait_for", separate_task_wait_for)

        async def receive_disconnect():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def complete_and_disconnect(message):
            nonlocal final_body_sent
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                disconnected.set()
                final_body_sent = True

        messages, _ = await invoke_observed(owner, after_body=receive_disconnect, send_hook=complete_and_disconnect)
        assert final_body_sent
        assert [message["status"] for message in messages if message["type"] == "http.response.start"] == [
            200 if response == "success" else 422
        ]
        assert owner.lease.events == ["response_sending", "response_send_complete"]
        owner.lease.assert_released()
        assert_no_other_tasks()

    asyncio.run(scenario())
