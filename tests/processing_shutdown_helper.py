"""Fixed source-CLI signal/reload observer; never installed as an app hook."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def record(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream)
    temporary.replace(path)


def fault_worker(arguments: list[str]) -> int:
    if len(arguments) != 5 or arguments[:2] != ["--einvoice-processing", "worker"]:
        return 70
    from app.processing import operations
    from app.processing.bootstrap import dispatch_if_requested

    def idle(*_args, **_kwargs):
        time.sleep(20)  # Independent failure-only test guard, below no host resource risk.
        os._exit(91)

    operations.execute_operation = idle
    result = dispatch_if_requested(arguments)
    return 70 if result is None else result


def create_app():
    import uvicorn

    from app import main
    from app.processing import native

    directory = Path(os.environ["SYNTHETIC_SHUTDOWN_DIRECTORY"])
    ordinary = native.role_command

    def command(role, arguments):
        if role == "worker":
            return [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "fault-worker",
                native.ROLE_FLAG,
                role,
                *arguments,
            ]
        return ordinary(role, arguments)

    native.role_command = command
    original_shutdown = uvicorn.Server.shutdown

    async def observed_shutdown(server, sockets=None):
        record(directory / f"shutdown-{os.getpid()}.json", {"monotonic": time.monotonic()})
        await original_shutdown(server, sockets)

    uvicorn.Server.shutdown = observed_shutdown

    def observe_ack():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with main.manager.lock:
                leases = tuple(main.manager.leases)
            if len(leases) == 1 and leases[0].python_deadline is not None:
                lease = leases[0]
                children = [lease.tree.supervisor, *lease.tree.roles.values()]
                if lease.tree.watchdog is not None:
                    children.append(lease.tree.watchdog)
                record(
                    directory / "active.json",
                    {"backend_pid": os.getpid(), "pids": [child.pid for child in children], "input_acknowledged": True},
                )
                return
            time.sleep(0.005)

    threading.Thread(target=observe_ack, daemon=True).start()
    # A broken test/reloader cannot leave this trusted backend alive indefinitely.
    fallback = threading.Timer(30, lambda: os._exit(92))
    fallback.daemon = True
    fallback.start()
    record(directory / f"backend-{os.getpid()}.json", {"pid": os.getpid()})
    return main.app


def main(arguments: list[str]) -> int:
    if arguments and arguments[0] == "fault-worker":
        return fault_worker(arguments[1:])
    if len(arguments) != 3 or arguments[0] not in {"signal", "reload"}:
        return 70
    mode, raw_directory, port = arguments
    directory = Path(raw_directory).resolve(strict=True)
    os.environ["SYNTHETIC_SHUTDOWN_DIRECTORY"] = str(directory)
    sys.path.insert(0, str(Path(__file__).parent))
    from app import cli

    real_run = cli.uvicorn.run

    def observe_run(app, **kwargs):
        assert app == "app.main:app"
        if kwargs["reload"]:
            kwargs.update(reload_dirs=[str(directory)], reload_delay=0.05)
        real_run("processing_shutdown_helper:create_app", factory=True, log_level="error", **kwargs)

    cli.uvicorn.run = observe_run
    sys.argv = ["e-rechnung-pruefer", "--host", "127.0.0.1", "--port", port]
    if mode == "reload":
        sys.argv.append("--reload")
    cli.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
