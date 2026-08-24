from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any

_SOCKET_PATH = os.environ.get("OPENSAC_KERNEL_SOCKET", "/tmp/opensac-kernel.sock")
_REQUEST_HEADER = struct.Struct("!I")
_FRAME_HEADER = struct.Struct("!cI")
_MAX_FRAME_BYTES = 1_000_000


def _read_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise EOFError("persistent interpreter disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def _connect() -> socket.socket:
    deadline = time.monotonic() + 30.0
    while True:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(_SOCKET_PATH)
            return connection
        except (FileNotFoundError, ConnectionRefusedError):
            connection.close()
            if time.monotonic() >= deadline:
                raise TimeoutError("persistent interpreter did not become ready") from None
            time.sleep(0.02)


def _request(program: str) -> dict[str, Any]:
    output = os.environ["OPENSAC_OUTPUT_PATH"]
    return {
        "program": program,
        "workspace": os.environ["OPENSAC_WORKSPACE"],
        "output": output,
        "ready": os.environ["OPENSAC_READY_PATH"],
        "environment": {
            "OPENSAC_SESSION_TOKEN": os.environ.get("OPENSAC_SESSION_TOKEN"),
            "OPENSAC_EXECUTION_ID": os.environ.get("OPENSAC_EXECUTION_ID"),
            "OPENSAC_OUTPUT_PATH": output,
            "OPENSAC_READY_PATH": os.environ["OPENSAC_READY_PATH"],
            "OPENSAC_WORKSPACE": os.environ["OPENSAC_WORKSPACE"],
        },
    }


def _write_result(value: dict[str, Any]) -> None:
    path = Path(os.environ["OPENSAC_KERNEL_RESULT_PATH"])
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: relay.py PROGRAM")
    payload = json.dumps(_request(sys.argv[1]), separators=(",", ":")).encode("utf-8")
    connection = _connect()
    with connection:
        connection.sendall(_REQUEST_HEADER.pack(len(payload)) + payload)
        while True:
            channel, size = _FRAME_HEADER.unpack(_read_exact(connection, _FRAME_HEADER.size))
            if size < 0 or size > _MAX_FRAME_BYTES:
                raise RuntimeError("persistent interpreter sent an invalid frame")
            frame = _read_exact(connection, size)
            if channel == b"O":
                os.write(1, frame)
            elif channel == b"E":
                os.write(2, frame)
            elif channel == b"R":
                result = json.loads(frame)
                if not isinstance(result, dict):
                    raise RuntimeError("persistent interpreter result is invalid")
                _write_result(result)
                exit_code = result.get("exit_code", 1)
                return exit_code if isinstance(exit_code, int) and 0 <= exit_code <= 255 else 1
            else:
                raise RuntimeError("persistent interpreter sent an unknown frame")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"persistent interpreter relay failed: {exc}", file=sys.stderr)
        raise SystemExit(125) from None
