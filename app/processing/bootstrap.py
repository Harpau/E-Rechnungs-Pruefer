"""Private early entry point shared by source Python and both frozen applications."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def dispatch_if_requested(arguments: Sequence[str]) -> int | None:
    from app.processing.native import ROLE_FLAG

    if not arguments or arguments[0] != ROLE_FLAG:
        if any(value.startswith(ROLE_FLAG) for value in arguments):
            return 70
        return None
    if len(arguments) < 5:
        return 70
    role = arguments[1]
    try:
        parent, input_handle, output_handle = (int(value) for value in arguments[2:5])
        if role not in {"supervisor", "worker", "java", "watchdog"}:
            return 70
        if sys.platform != "win32":
            from app.processing.posix import bind_parent

            bind_parent(parent)
        from app.processing.native import inherited_file

        with inherited_file(input_handle, "rb") as incoming, inherited_file(output_handle, "wb") as outgoing:
            if role == "watchdog":
                from app.processing.posix import apply_limits
                from app.processing.watchdog import run_watchdog

                if len(arguments) != 9:
                    return 70
                apply_limits(memory_headroom=64 * 1024**2, cpu_seconds=30)
                run_watchdog(
                    parent_pid=parent,
                    parent_pgid=int(arguments[5]),
                    supervisor_pid=int(arguments[6]),
                    session_id=int(arguments[7]),
                    liveness_fd=incoming.fileno(),
                    ready_fd=outgoing.fileno(),
                    deadline=float(arguments[8]),
                )
                return 0
            expected_lengths = {"worker": {5}, "java": {7}, "supervisor": {7, 11}}
            if len(arguments) not in expected_lengths[role]:
                return 70
            # Startup has no document input. Bound the interval before the
            # independent tree controls are installed on POSIX as well.
            if sys.platform != "win32":
                import signal

                signal.setitimer(signal.ITIMER_REAL, 15)
            from app.processing.protocol import read_control

            setup = read_control(incoming)
            if setup.get("type") != "setup":
                return 70
            if role == "worker":
                from app.processing.worker import run_worker

                run_worker(incoming, outgoing, setup)
            elif role == "java":
                from app.processing.supervisor import run_java_launcher

                return run_java_launcher(incoming, outgoing, setup, int(arguments[5]), int(arguments[6]))
            else:
                from app.processing.supervisor import run_supervisor

                run_supervisor(incoming, outgoing, setup, [int(value) for value in arguments[5:]])
            return 0
    except BaseException:
        # No invoice-derived traceback or secret environment enters stderr.
        return 70


if __name__ == "__main__":
    status = dispatch_if_requested(sys.argv[1:])
    raise SystemExit(70 if status is None else status)
