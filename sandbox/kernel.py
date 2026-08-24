from __future__ import annotations

import builtins
import codeop
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
from contextlib import suppress
from pathlib import Path
from typing import Any

_SOCKET_PATH = os.environ.get("OPENSAC_KERNEL_SOCKET", "/tmp/opensac-kernel.sock")
_WORKSPACE_ROOT = Path(os.environ.get("OPENSAC_KERNEL_WORKSPACE_ROOT", "/workspace")).resolve()
_REQUEST_HEADER = struct.Struct("!I")
_FRAME_HEADER = struct.Struct("!cI")
_MAX_REQUEST_BYTES = 64 * 1024
_ENVIRONMENT_KEYS = (
    "OPENSAC_SESSION_TOKEN",
    "OPENSAC_EXECUTION_ID",
    "OPENSAC_OUTPUT_PATH",
    "OPENSAC_READY_PATH",
    "OPENSAC_WORKSPACE",
)


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("kernel request ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_request(connection: socket.socket) -> dict[str, Any]:
    (size,) = _REQUEST_HEADER.unpack(_read_exact(connection, _REQUEST_HEADER.size))
    if size <= 0 or size > _MAX_REQUEST_BYTES:
        raise ValueError("kernel request size is invalid")
    value = json.loads(_read_exact(connection, size))
    if not isinstance(value, dict):
        raise ValueError("kernel request must be a JSON object")
    return value


def _send_frame(
    connection: socket.socket,
    lock: threading.Lock,
    channel: bytes,
    payload: bytes,
) -> None:
    with lock:
        connection.sendall(_FRAME_HEADER.pack(channel, len(payload)) + payload)


def _workspace_path(value: Any, field: str) -> Path:
    path = Path(str(value)).resolve()
    if not path.is_relative_to(_WORKSPACE_ROOT):
        raise ValueError(f"{field} must be inside the kernel workspace")
    return path


def _pump(
    descriptor: int,
    connection: socket.socket,
    lock: threading.Lock,
    channel: bytes,
) -> None:
    try:
        while chunk := os.read(descriptor, 64 * 1024):
            try:
                _send_frame(connection, lock, channel, chunk)
            except OSError:
                # The host may have killed a relay after a timeout or output
                # limit. Keep draining until the container itself is removed.
                continue
    finally:
        os.close(descriptor)


def _system_exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=sys.stderr)
    return 1


class PersistentKernel:
    def __init__(self) -> None:
        self.namespace: dict[str, Any] = {
            "__name__": "__main__",
            "__package__": None,
            "__builtins__": builtins,
        }
        self.compiler = codeop.CommandCompiler()
        self._stdout = sys.stdout
        self._stderr = sys.stderr

    def execute(self, connection: socket.socket, request: dict[str, Any]) -> bool:
        program = _workspace_path(request.get("program"), "program")
        workspace = _workspace_path(request.get("workspace"), "workspace")
        _workspace_path(request.get("output"), "output")
        ready = _workspace_path(request.get("ready"), "ready")
        environment = request.get("environment")
        if not isinstance(environment, dict):
            raise ValueError("kernel environment must be a JSON object")

        source = program.read_text(encoding="utf-8")
        code = self.compiler(source, str(program), "exec")
        if code is None:
            raise SyntaxError("incomplete Python input")

        prior_environment = {key: os.environ.get(key) for key in _ENVIRONMENT_KEYS}
        for key in _ENVIRONMENT_KEYS:
            value = environment.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)

        send_lock = threading.Lock()
        baseline_threads = frozenset(id(item) for item in threading.enumerate())
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        stdout_pump = threading.Thread(
            target=_pump,
            args=(stdout_read, connection, send_lock, b"O"),
            name="opensac-stdout-pump",
            daemon=True,
        )
        stderr_pump = threading.Thread(
            target=_pump,
            args=(stderr_read, connection, send_lock, b"E"),
            name="opensac-stderr-pump",
            daemon=True,
        )
        stdout_pump.start()
        stderr_pump.start()

        exit_code = 0
        try:
            os.dup2(stdout_write, 1)
            os.dup2(stderr_write, 2)
            os.close(stdout_write)
            os.close(stderr_write)
            os.chdir(workspace)
            self.namespace["__file__"] = str(program)
            ready.write_text(str(time.time_ns()), encoding="utf-8")
            exec(code, self.namespace, self.namespace)
        except SystemExit as exc:
            exit_code = _system_exit_code(exc)
        except BaseException:
            exit_code = 1
            traceback.print_exc()
        finally:
            with suppress(BaseException):
                sys.stdout.flush()
            with suppress(BaseException):
                sys.stderr.flush()
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
            sys.stdout = self._stdout
            sys.stderr = self._stderr
            os.chdir(_WORKSPACE_ROOT)
            for key, value in prior_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        stdout_pump.join()
        stderr_pump.join()
        leftover_threads = [
            item
            for item in threading.enumerate()
            if id(item) not in baseline_threads and item.is_alive()
        ]
        loss_reason = "background_threads" if leftover_threads else None
        result = {
            "protocol": 1,
            "complete": True,
            "exit_code": exit_code,
            "namespace_symbol_count": sum(not name.startswith("__") for name in self.namespace),
            "interpreter_loss_reason": loss_reason,
        }
        _send_frame(
            connection,
            send_lock,
            b"R",
            json.dumps(result, separators=(",", ":")).encode("utf-8"),
        )
        return loss_reason is None


def main() -> None:
    socket_path = Path(_SOCKET_PATH)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o600)
    server.listen(1)
    kernel = PersistentKernel()
    while True:
        connection, _ = server.accept()
        healthy = True
        with connection:
            try:
                healthy = kernel.execute(connection, _read_request(connection))
            except BaseException:
                traceback.print_exc()
                healthy = False
        if not healthy:
            os._exit(70)


if __name__ == "__main__":
    main()
