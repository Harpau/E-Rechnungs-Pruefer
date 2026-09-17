"""Fixed synthetic active-role faults; no pytest, arbitrary commands or target PIDs.

The Java-shaped child sleeps at most ten seconds. Its command alone is replaced
inside this test helper; production launcher, limits, GO, IPC, private temporary
files and the actual ASGI/native Lease remain unchanged. This is not JVM testing.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
RECORD_LIMIT = 128 * 1024
CASE_SECONDS = 35
sys.path.insert(0, str(ROOT))


def cases_for_platform(platform: str) -> tuple[str, ...]:
    if platform not in {"linux", "darwin", "win32"}:
        raise ValueError("unsupported native platform")
    cases = ("cancel", "disconnect", "worker", "supervisor", "parent")
    return (*cases, "watchdog") if platform == "darwin" else cases


def record(path: Path, value: object) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    if len(raw) > RECORD_LIMIT:
        raise RuntimeError("bounded record exceeded")
    # Atomic publication, exclusive final ownership (hardlink does not replace).
    temporary = path.with_name(path.name + ".pending")
    with temporary.open("xb") as stream:
        stream.write(raw)
        stream.flush()
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def read_record(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(RECORD_LIMIT + 1)
    if len(raw) > RECORD_LIMIT:
        raise RuntimeError("bounded record exceeded")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("invalid record")
    return value


def directory_identity(path: Path) -> dict[str, Any]:
    information = path.lstat()
    if not stat.S_ISDIR(information.st_mode) or getattr(information, "st_file_attributes", 0) & 0x400:
        raise RuntimeError("temporary directory identity is not regular")
    if os.name == "posix" and (information.st_uid != os.getuid() or information.st_mode & 0o077):
        raise RuntimeError("temporary directory permissions are not private")
    return {
        "path": str(path),
        "dev": information.st_dev,
        "ino": information.st_ino,
        "mode": information.st_mode,
        "uid": information.st_uid,
        "gid": information.st_gid,
    }


def cleanup_owned_temp(
    runtime: Path, identity: dict[str, Any], *, ended: bool, evidence: Path | None = None
) -> dict[str, Any]:
    if not ended:
        raise RuntimeError("native end is not confirmed")
    path = Path(identity["path"])
    if path.parent != runtime or not path.name.startswith("einvoice-kosit-"):
        raise RuntimeError("temporary directory outside owned scope")
    if not path.exists() and not path.is_symlink():
        return {"present_after_native_end": False, "harness_removed": False}
    if directory_identity(path) != identity:
        raise RuntimeError("temporary directory identity changed")
    # Record names/sizes/types, never invoice bytes. Reject links/reparse points;
    # cleanup authority does not extend beyond this fixed synthetic directory.
    entries: list[dict[str, Any]] = []
    for current, directories, files in os.walk(path, followlinks=False, onerror=_walk_error):
        for name in sorted([*directories, *files]):
            child = Path(current) / name
            info = child.lstat()
            if len(entries) >= 32 or info.st_nlink != 1 and stat.S_ISREG(info.st_mode):
                raise RuntimeError("unexpected temporary payload inventory")
            if getattr(info, "st_file_attributes", 0) & 0x400 or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise RuntimeError("temporary payload contains unsafe link or type")
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise RuntimeError("temporary payload permissions changed")
            entries.append(
                {
                    "relative_path": str(child.relative_to(path)),
                    "size": info.st_size,
                    "mode": info.st_mode,
                    "dev": info.st_dev,
                    "ino": info.st_ino,
                }
            )
    # Persist the retained state separately before the explicitly scoped harness
    # cleanup; an interrupted cleanup never erases this distinction.
    if evidence is not None:
        record(evidence, {"identity": identity, "native_end_confirmed": True, "retained_inventory": entries})
    # No product cleanup is asserted: a hard parent death can leave this tree.
    shutil.rmtree(path)
    return {"present_after_native_end": True, "harness_removed": True, "retained_inventory": entries}


def _walk_error(error: OSError) -> None:
    raise error


class BoundExits:
    """Read-only kernel handles; never signal a numeric PID learned from a file."""

    def __init__(self, pids: list[int]) -> None:
        if not pids or len(set(pids)) != len(pids) or any(type(pid) is not int or pid <= 1 for pid in pids):
            raise RuntimeError("invalid bound process inventory")
        self.handles: list[Any] = []
        self.identities: list[dict[str, int]] = []
        self.posix: Any = None
        self.api: Any = None
        try:
            if sys.platform == "win32":
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
                api.OpenProcess.restype = ctypes.c_void_p
                api.GetProcessTimes.argtypes = [ctypes.c_void_p, *([ctypes.c_void_p] * 4)]
                api.GetProcessTimes.restype = ctypes.c_int
                api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                api.WaitForSingleObject.restype = ctypes.c_uint32
                api.CloseHandle.argtypes = [ctypes.c_void_p]
                api.CloseHandle.restype = ctypes.c_int
                self.api = api
                for pid in pids:
                    handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
                    if not handle:
                        raise OSError("process identity unavailable")
                    self.handles.append(handle)
                    times = [ctypes.c_uint64() for _ in range(4)]
                    if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                        raise OSError("process creation identity unavailable")
                    self.identities.append({"pid": pid, "creation_filetime": times[0].value})
            else:
                from app.processing.native import ExitBindings

                self.posix = ExitBindings(pids)
                self.identities = [{"pid": pid} for pid in pids]
        except BaseException:
            self.close()
            raise

    def all_running(self) -> bool:
        if self.posix is not None:
            any_or_all_ended = self.posix.exited()
            if self.posix.queue is not None:
                # Darwin's aggregate exited() means all ended; pending records
                # each exit, so an individual early exit must also reject GO.
                return len(self.posix.pending) == len(self.identities)
            return not any_or_all_ended
        for handle in self.handles:
            status = self.api.WaitForSingleObject(handle, 0)
            if status == 0:
                return False
            if status != 258:
                raise OSError("process liveness observation failed")
        return True

    def wait(self, deadline: float) -> bool:
        if self.posix is not None:
            return bool(self.posix.wait(deadline))
        for handle in self.handles:
            status = self.api.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
            if status == 258:
                return False
            if status != 0:
                raise OSError("process exit observation failed")
        return True

    def close(self) -> None:
        if self.posix is not None:
            self.posix.close()
            self.posix = None
        while self.handles:
            if not self.api.CloseHandle(self.handles.pop()):
                raise OSError("process observer close failed")


def require_active(*, request_done: bool, all_running: bool) -> None:
    if request_done or not all_running:
        raise RuntimeError("fault target is no longer active")


def ended_in_budget(ended: bool, started: float, observed: float) -> bool:
    return ended and 0 <= observed - started <= 5


def temporary_result_allowed(case: str, result: dict[str, Any]) -> bool:
    return case == "parent" or result["present_after_native_end"] is False


def multipart(payload: bytes, *, official: bool) -> bytes:
    field = b'--synthetic\r\nContent-Disposition: form-data; name="official"\r\n\r\ntrue\r\n' if official else b""
    return (
        field
        + b'--synthetic\r\nContent-Disposition: form-data; name="file"; filename="synthetic.xml"\r\n'
        + b"Content-Type: application/xml\r\n\r\n"
        + payload
        + b"\r\n--synthetic--\r\n"
    )


async def asgi_request(application: Any, path: str, payload: bytes, disconnect: asyncio.Event) -> dict[str, Any]:
    from starlette.types import Message, Scope

    body = multipart(payload, official=path != "/api/xml")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "server": ("127.0.0.1", 8080),
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"127.0.0.1:8080"),
            (b"content-type", b"multipart/form-data; boundary=synthetic"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    sent = False
    response: dict[str, Any] = {"status": None, "body_size": 0}
    digest = hashlib.sha256()

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            response["body_size"] += len(chunk)
            if response["body_size"] > 2 * 1024**2:
                raise RuntimeError("bounded synthetic response exceeded")
            digest.update(chunk)

    try:
        await application(scope, receive, send)
    except asyncio.CancelledError:
        response["cancelled"] = True
    response["sha256"] = digest.hexdigest()
    return response


def java_role(directory: Path, arguments: list[str]) -> int:
    if len(arguments) != 7 or arguments[:2] != ["--einvoice-processing", "java"]:
        return 70
    from app.processing import kosit_runtime
    from app.processing.bootstrap import dispatch_if_requested

    original = kosit_runtime.prepare_java_command

    def command(settings: Any, temp_directory: Path, budgets: Any) -> list[str]:
        original(settings, temp_directory, budgets)
        return [sys.executable, "-I", str(SCRIPT), "--java-stub", str(directory), str(temp_directory)]

    kosit_runtime.prepare_java_command = command
    result = dispatch_if_requested(arguments)
    return 70 if result is None else result


def java_stub(directory: Path, temporary: Path) -> int:
    invoice = temporary / "invoice.xml"
    if not invoice.is_file() or invoice.stat().st_size > 64 * 1024:
        return 71
    record(
        directory / "java-active.json",
        {
            "pid": os.getpid(),
            "parent_pid": os.getppid(),
            "temporary": directory_identity(temporary),
            "go_observed": True,
            "invoice_size": invoice.stat().st_size,
        },
    )
    time.sleep(10)
    return 92  # A timed-out synthetic sleeper is never a lifecycle PASS.


async def controller(case: str, directory: Path) -> dict[str, Any]:
    from app import main
    from app.configuration import Settings
    from app.processing import native
    from app.processing.manager import ProcessingManager

    runtime = directory / "runtime"
    runtime.mkdir(mode=0o700)
    tempfile.tempdir = str(runtime)
    jar = directory / "synthetic.jar"
    with zipfile.ZipFile(jar, "x") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nMain-Class: SyntheticOnly\n")
    scenario = directory / "synthetic-scenario.xml"
    scenario.write_text("<synthetic/>", encoding="utf-8")
    manager = ProcessingManager()
    main.manager = manager
    main.settings = Settings(kosit_java_bin=sys.executable, kosit_validator_jar=jar, kosit_scenarios=(scenario,))
    ordinary_command = native.role_command

    def command(role: str, arguments: Any) -> list[str]:
        if role == "java":
            return [
                sys.executable,
                "-I",
                str(SCRIPT),
                "--java-role",
                str(directory),
                native.ROLE_FLAG,
                role,
                *arguments,
            ]
        return ordinary_command(role, arguments)

    native.role_command = command
    payload = (ROOT / "app/examples/cii-rechnung-demo.xml").read_bytes()
    disconnect = asyncio.Event()
    request = asyncio.create_task(asgi_request(main.app, "/api/analyze", payload, disconnect))
    deadline = time.monotonic() + 15
    try:
        while not (directory / "java-active.json").exists():
            if request.done():
                raise RuntimeError("request ended before active Java-shaped role")
            if time.monotonic() >= deadline:
                raise TimeoutError("active startup deadline")
            await asyncio.sleep(0.01)
        with manager.lock:
            leases = tuple(manager.leases)
        if len(leases) != 1:
            raise RuntimeError("active lease inventory mismatch")
        lease = leases[0]
        if not lease.ready.is_set() or lease.ready_manifest is None or lease.python_deadline is not None:
            raise RuntimeError("Java wait after input ACK is not established")
        roles = {"supervisor": lease.tree.supervisor, **lease.tree.roles}
        if lease.tree.watchdog is not None:
            roles["watchdog"] = lease.tree.watchdog
        pids = {name: child.pid for name, child in roles.items() if child is not None}
        record(
            directory / "active.json",
            {
                "controller_pid": os.getpid(),
                "roles": pids,
                "input_acknowledged": True,
                "ready": lease.ready_manifest,
                "java": read_record(directory / "java-active.json"),
            },
        )
        while not (directory / "go.json").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("observer binding deadline")
            await asyncio.sleep(0.005)
        # Fresh kernel observations before the actual fault; the historical
        # marker alone is not evidence that the bounded sleeper still runs.
        live_pids = list(pids.values())
        if sys.platform == "win32":
            live_pids.append(read_record(directory / "java-active.json")["pid"])
        check = BoundExits(live_pids)
        try:
            require_active(request_done=request.done(), all_running=check.all_running())
        finally:
            check.close()
        started = time.monotonic()
        record(directory / "fault.json", {"case": case, "monotonic": started})
        if case == "parent":
            await asyncio.sleep(12)  # Outer controller terminates its held Popen.
            raise RuntimeError("parent-death action absent")
        if case == "cancel":
            request.cancel()
        elif case == "disconnect":
            disconnect.set()  # Actual ASGI http.disconnect into the real middleware/Lease.
        else:
            child = roles[case]
            if child is None:
                raise RuntimeError("missing fixed role")
            child.kill()
        response = await asyncio.wait_for(request, 6)
        clean = lease.tree.cleaned and not lease.poisoned and manager.active_count == 0
        if not clean:
            raise RuntimeError("native cleanup or capacity recovery not confirmed")
        if response["status"] == 200:
            raise RuntimeError("interrupted analysis unexpectedly returned success")
        fresh_payload = b"<synthetic-byte-exact-export/>\n"
        fresh = await asyncio.wait_for(asgi_request(main.app, "/api/xml", fresh_payload, asyncio.Event()), 15)
        if fresh["status"] != 200 or fresh["sha256"] != hashlib.sha256(fresh_payload).hexdigest():
            raise RuntimeError("fresh XML export after fault failed")
        return {
            "passed": True,
            "response": response,
            "fresh_xml": fresh,
            "active_count": manager.active_count,
            "tree_cleaned": lease.tree.cleaned,
            "poisoned": lease.poisoned,
        }
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        manager.shutdown()


def wait_record(path: Path, process: subprocess.Popen[bytes], deadline: float) -> dict[str, Any]:
    while not path.exists():
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError(f"missing bounded marker: {path.name}")
        time.sleep(0.01)
    return read_record(path)


def run_case(case: str, directory: Path) -> dict[str, Any]:
    from app.processing.native import child_environment

    directory.mkdir(mode=0o700)
    binding: BoundExits | None = None
    active: dict[str, Any] | None = None
    ended = False
    report: dict[str, Any] = {"case": case, "passed": False}
    with (directory / "stdout.txt").open("xb") as stdout, (directory / "stderr.txt").open("xb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "--controller", case, str(directory)],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=child_environment(),
            start_new_session=os.name == "posix",
        )
        try:
            active = wait_record(directory / "active.json", process, time.monotonic() + 18)
            if active["controller_pid"] != process.pid or active["input_acknowledged"] is not True:
                raise RuntimeError("controller binding mismatch")
            roles = active["roles"]
            expected = {"supervisor", "worker", "java"} | ({"watchdog"} if sys.platform == "darwin" else set())
            if set(roles) != expected:
                raise RuntimeError("fixed role inventory mismatch")
            stub = active["java"]
            if stub["go_observed"] is not True:
                raise RuntimeError("Java-shaped role not running")
            pids = list(roles.values())
            if sys.platform == "win32":
                if stub["parent_pid"] != roles["java"] or stub["pid"] in pids:
                    raise RuntimeError("Java-shaped child identity mismatch")
                pids.append(stub["pid"])
            elif stub["pid"] != roles["java"] or stub["parent_pid"] != process.pid:
                raise RuntimeError("exec Java-shaped role identity mismatch")
            binding = BoundExits(pids)
            report["bound_identities"] = binding.identities
            report["active"] = active
            require_active(request_done=process.poll() is not None, all_running=binding.all_running())
            record(directory / "go.json", {"bound": True})
            fault = wait_record(directory / "fault.json", process, time.monotonic() + 2)
            started = fault["monotonic"]
            if case == "parent":
                require_active(request_done=process.poll() is not None, all_running=binding.all_running())
                started = time.monotonic()
                process.kill()
            ended = binding.wait(started + 5)
            observed = time.monotonic()
            in_budget = ended_in_budget(ended, started, observed)
            report["roles_ended_within_five_seconds"] = in_budget
            report["observed_role_end_seconds"] = observed - started
            if not in_budget:
                raise RuntimeError("active role survived five-second native cleanup boundary")
            if case == "parent":
                process.wait(timeout=2)
                report["parent_returncode"] = process.returncode
            else:
                result = wait_record(directory / "result.json", process, time.monotonic() + 18)
                report["controller_result"] = result
                if result.get("passed") is not True:
                    raise RuntimeError("controller fault result failed")
                process.wait(timeout=2)
                if process.returncode != 0:
                    raise RuntimeError("controller exited unsuccessfully")
            report["passed"] = True
        except Exception as exc:
            report["failure_class"] = type(exc).__name__
            report["failure"] = str(exc)[:256]
        finally:
            # Own Popen only; no unbound PID or foreign process-group signals.
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            if binding is not None:
                ended = binding.wait(time.monotonic() + 3) or ended
                report["all_bound_roles_ended_before_harness_cleanup"] = ended
                binding.close()
            if active is not None:
                try:
                    report["temporary_cleanup"] = cleanup_owned_temp(
                        directory / "runtime",
                        active["java"]["temporary"],
                        ended=ended,
                        evidence=directory / "temporary-before-harness-cleanup.json",
                    )
                    if not temporary_result_allowed(case, report["temporary_cleanup"]):
                        report["passed"] = False
                        report["temporary_cleanup_error"] = "ProductTemporaryDirectoryRetained"
                except Exception as exc:
                    report["passed"] = False
                    report["temporary_cleanup_error"] = type(exc).__name__
    record(directory / "outer-result.json", report)
    return report


def run_catalog(output: Path) -> dict[str, Any]:
    output = output.absolute()
    raw = output.with_suffix(".raw")
    if output.exists() or raw.exists():
        raise FileExistsError("evidence output already exists")
    raw.mkdir(mode=0o700, parents=True)
    results = [run_case(case, raw / case) for case in cases_for_platform(sys.platform)]
    report = {
        "schema_version": 1,
        "passed": all(result["passed"] for result in results),
        "platform": sys.platform,
        "python": sys.version,
        "pid": os.getpid(),
        "cases": results,
        "scope": "source runtime; actual ASGI disconnect and native leases; fixed ten-second Java-shaped helper, not JVM",
        "helper_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
        "source_sha256_scope": "selected lifecycle implementation files, not complete workspace",
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "app/processing").glob("*.py"))
        },
    }
    record(output, report)
    return report


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) >= 3 and arguments[0] == "--java-role":
        return java_role(Path(arguments[1]).resolve(strict=True), arguments[2:])
    if len(arguments) == 3 and arguments[0] == "--java-stub":
        return java_stub(Path(arguments[1]).resolve(strict=True), Path(arguments[2]).resolve(strict=True))
    if len(arguments) == 3 and arguments[0] == "--controller" and arguments[1] in cases_for_platform(sys.platform):
        directory = Path(arguments[2]).resolve(strict=True)
        # Independent process-wide guard survives event-loop/thread stalls.
        timer = threading.Timer(CASE_SECONDS, lambda: os._exit(93))
        timer.daemon = True
        timer.start()
        try:
            result = asyncio.run(controller(arguments[1], directory))
        except BaseException as exc:
            result = {"passed": False, "failure_class": type(exc).__name__, "failure": str(exc)[:256]}
        record(directory / "result.json", result)
        timer.cancel()
        return 0 if result["passed"] else 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    return 0 if run_catalog(parsed.output)["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
