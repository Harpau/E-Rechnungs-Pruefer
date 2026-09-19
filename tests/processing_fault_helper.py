"""Fixed synthetic subprocess faults, reachable only from the test command shim.

The real private bootstrap applies the normal parent/job bindings and limits.
Only the trusted operation is replaced; there is no product/environment hook.
No fault allocates more than one 128-MiB bytearray or runs beyond six seconds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _bounded_stall(*, busy: bool) -> None:
    until = time.monotonic() + 6
    while time.monotonic() < until:
        if not busy:
            time.sleep(0.01)
    os._exit(91)  # An independent test guard, never a successful budget proof.


def main(arguments: list[str]) -> int:
    modes = {"cpu", "memory", "oversized_output", "partial_ipc", "idle", "java_lingering_child"}
    if len(arguments) < 3 or arguments[0] not in modes or arguments[1] != "--einvoice-processing":
        return 70
    role = arguments[2]
    if (role == "worker" and len(arguments) != 6) or (role == "java" and len(arguments) != 8):
        return 70
    if role not in {"worker", "java"} or (role == "java" and arguments[0] != "java_lingering_child"):
        return 70
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.processing import operations, worker
    from app.processing.bootstrap import dispatch_if_requested
    from app.processing.result import OperationResult

    mode = arguments[0]
    if role == "java":
        if sys.platform != "win32":
            return 70
        from app.processing import supervisor
        from app.processing.native import child_environment, inherited_file, python_executable
        from app.processing.protocol import read_control, write_control

        def launcher(incoming, outgoing, setup, stdout_handle, stderr_handle):
            with inherited_file(stdout_handle, "wb"), inherited_file(stderr_handle, "wb"):
                write_control(
                    outgoing,
                    {
                        "type": "ready",
                        "role": "java",
                        "protocol": 2,
                        "limits": {"job_memory_bytes": setup["budgets"]["java_memory_bytes"]},
                    },
                )
                if read_control(incoming) != {"type": "go"}:
                    return 70
                # The fixed child inherits the actual outer/role Windows jobs
                # atomically. Its six-second self-exit is only a test guard.
                subprocess.Popen(
                    [python_executable(), "-I", "-c", "import time; time.sleep(6)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env=child_environment(),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return 12

        supervisor.run_java_launcher = launcher
        result = dispatch_if_requested(arguments[1:])
        return 70 if result is None else result

    class OversizedResult:
        body = b"x"

        def metadata(self):
            return {
                "status_code": 200,
                "media_type": "application/xml",
                "headers": {"Content-Disposition": 'attachment; filename="example.xml"'},
                "body_size": 25 * 1024**2 + 1,
            }

    def operation(_operation, data, *_args, **_kwargs):
        if mode == "java_lingering_child":
            result = _kwargs["official_validator"](data, "example.xml")
            proof = {
                "executed": result["executed"],
                "accepted": result["accepted"],
                "finding": result["findings"][0]["id"],
            }
            return OperationResult(json.dumps(proof).encode("ascii"), "application/json", {})
        if mode in {"cpu", "idle"}:
            _bounded_stall(busy=mode == "cpu")
        if mode == "memory":
            payload = bytearray(128 * 1024**2)
            del payload
            raise RuntimeError("Synthetic allocation unexpectedly escaped the selected native limit")
        if mode == "oversized_output":
            return OversizedResult()
        return OperationResult(data, "application/xml", {"Content-Disposition": 'attachment; filename="example.xml"'})

    operations.execute_operation = operation
    if mode == "partial_ipc":

        def partial_payload(stream, _payload):
            # Less than a complete five-byte header. The real broker blocks
            # reading, while the independent parent deadline must remain live.
            stream.write(b"\x02\x00\x00")
            stream.flush()
            _bounded_stall(busy=False)

        worker.write_payload = partial_payload
    result = dispatch_if_requested(arguments[1:])
    return 70 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
