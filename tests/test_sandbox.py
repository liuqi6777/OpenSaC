from __future__ import annotations

import asyncio
import json
import sys

import pytest

from opensac.sandbox import (
    DockerSandbox,
    SandboxRequest,
    SandboxResult,
    UnsafeCodeError,
    validate_code,
)
from opensac.sandbox.docker import (
    BoundedProcessOutput,
    DockerImageContractVerifier,
    SandboxImageContractError,
    broker_socket_mount_args,
    read_bounded_process_output,
)
from opensac.sandbox.docker_core import ExecutionWorkspace, broker_socket_container_path


class _CompletedProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def test_validator_accepts_sdk_orchestration() -> None:
    validate_code("from opensac_sdk import sdk\nhits = sdk.search('query')")


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess",
        "from socket import socket",
        "eval('1 + 1')",
        "object.__subclasses__()",
        "().__class__.__bases__[0].__subclasses__()",
        "print(sdk.search.__globals__)",
        "print(open.__self__.__dict__)",
    ],
)
def test_validator_blocks_dangerous_code(code) -> None:
    with pytest.raises(UnsafeCodeError):
        validate_code(code)


def test_validator_allows_reporting_an_exception_type() -> None:
    """`type(exc).__name__` is how Python code names an error.

    Generated pipelines wrap retrieval in try/except constantly, so rejecting
    the whole program over its error message burned a turn per occurrence and
    taught the control model nothing about the real rule.
    """
    validate_code(
        "from opensac_sdk import sdk\n"
        "try:\n"
        "    hits = sdk.search('query')\n"
        "except Exception as exc:\n"
        "    print(f'{type(exc).__name__}: {exc}')\n"
        "    print(sdk.__doc__)\n"
        "    print(sdk.search.__doc__)\n"
        "    print(sdk.search.many.__doc__)\n"
    )


def test_rejection_names_the_offending_dunder() -> None:
    with pytest.raises(UnsafeCodeError, match="__class__"):
        validate_code("x = object().__class__")


def test_docker_command_has_security_boundaries(tmp_path) -> None:
    socket = tmp_path / "broker.sock"
    socket.touch()
    workspace = tmp_path / "workspace"
    sandbox = DockerSandbox(image="opensac-test", broker_socket=socket)
    command = sandbox.command(SandboxRequest("pass", workspace, "secret"))
    joined = " ".join(command)
    assert "--network none" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "OPENSAC_SESSION_TOKEN=secret" in joined
    assert "OPENSAC_READY_PATH=/workspace/.opensac-output.json.ready" in joined
    assert str(socket.resolve().parent) in joined
    assert "OPENSAC_BROKER_SOCKET=/run/opensac/broker.sock" in joined
    assert all(argument in command for argument in broker_socket_mount_args(socket))
    assert str(workspace.resolve() / ".opensac-container-id") not in joined


def test_broker_socket_mount_uses_dedicated_directory_on_docker_desktop(tmp_path) -> None:
    socket = tmp_path / "broker" / "custom.sock"
    destination = "/run/opensac/broker.sock"

    assert broker_socket_mount_args(socket, platform="darwin") == [
        "--volume",
        f"{socket.resolve().parent}:/run/opensac:ro",
        "--group-add",
        "0",
    ]
    assert broker_socket_container_path(socket, platform="darwin") == "/run/opensac/custom.sock"
    assert broker_socket_mount_args(socket, platform="linux") == [
        "--mount",
        f"type=bind,src={socket.resolve()},dst={destination},readonly",
    ]
    assert broker_socket_container_path(socket, platform="linux") == destination


def test_docker_sandbox_accepts_an_explicit_docker_host_platform(tmp_path) -> None:
    socket = tmp_path / "broker.sock"
    socket.touch()
    sandbox = DockerSandbox(
        image="opensac-test",
        broker_socket=socket,
        docker_host_platform="darwin",
    )

    command = sandbox.command(SandboxRequest("pass", tmp_path / "workspace", "secret"))

    assert all(
        argument in command for argument in broker_socket_mount_args(socket, platform="darwin")
    )


async def test_sandbox_image_contract_is_inspected_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def create_process(*command: str, **_: object) -> _CompletedProcess:
        calls.append(command)
        return _CompletedProcess(stdout=b"14\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    verifier = DockerImageContractVerifier("opensac-test")

    await asyncio.gather(verifier.ensure_compatible(), verifier.ensure_compatible())
    await verifier.ensure_compatible()

    assert len(calls) == 1
    assert calls[0][:3] == ("docker", "image", "inspect")
    assert calls[0][-1] == "opensac-test"


async def test_missing_sandbox_image_is_pulled_before_contract_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    results = iter(
        [
            _CompletedProcess(returncode=1, stderr=b"No such image: published:0.6.0\n"),
            _CompletedProcess(stdout=b"pulled\n"),
            _CompletedProcess(stdout=b"14\n"),
        ]
    )

    async def create_process(*command: str, **_: object) -> _CompletedProcess:
        calls.append(command)
        return next(results)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    await DockerImageContractVerifier("published:0.6.0").ensure_compatible()

    assert [call[1:3] for call in calls] == [
        ("image", "inspect"),
        ("pull", "published:0.6.0"),
        ("image", "inspect"),
    ]


def test_execution_warnings_have_an_independent_output_budget(tmp_path) -> None:
    workspace = ExecutionWorkspace.prepare(SandboxRequest("pass", tmp_path, "secret"))
    submitted = {"output": {"answer": "x" * 100}, "citations": []}
    warning = {
        "code": "external_result_failure",
        "method": "content.grep",
        "success_count": 1,
        "failure_count": 1,
        "failures": [{"code": "provider_timeout", "message": "timed out"}],
        "omitted_failure_count": 0,
    }
    workspace.output_path.write_text(
        json.dumps({**submitted, "warnings": [warning]}, separators=(",", ":")),
        encoding="utf-8",
    )

    value, stderr = workspace.read_output(
        max_output_bytes=len(json.dumps(submitted, separators=(",", ":")).encode())
    )

    assert value == {**submitted, "warnings": [warning]}
    assert stderr == ""


def test_oversized_execution_warnings_do_not_discard_valid_output(tmp_path) -> None:
    workspace = ExecutionWorkspace.prepare(SandboxRequest("pass", tmp_path, "secret"))
    submitted = {"output": {"answer": 42}, "citations": []}
    workspace.output_path.write_text(
        json.dumps({**submitted, "warnings": ["x" * 5_000]}, separators=(",", ":")),
        encoding="utf-8",
    )

    value, stderr = workspace.read_output(
        max_output_bytes=len(json.dumps(submitted, separators=(",", ":")).encode())
    )

    assert value == submitted
    assert stderr == ""


async def test_sandbox_image_pull_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = iter(
        [
            _CompletedProcess(returncode=1, stderr=b"No such object: private:0.6.0\n"),
            _CompletedProcess(returncode=1, stderr=b"denied\n"),
        ]
    )

    async def create_process(*_: str, **__: object) -> _CompletedProcess:
        return next(results)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(SandboxImageContractError, match="Could not pull.*denied"):
        await DockerImageContractVerifier("private:0.6.0").ensure_compatible()


async def test_cold_sandbox_rejects_stale_image_before_workspace_setup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    async def create_process(*command: str, **_: object) -> _CompletedProcess:
        calls.append(command)
        return _CompletedProcess(stdout=b"2\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    socket = tmp_path / "broker.sock"
    socket.touch()
    workspace = tmp_path / "workspace"
    sandbox = DockerSandbox(image="opensac-stale", broker_socket=socket)

    result = await sandbox.execute(SandboxRequest("pass", workspace, "secret"))

    assert result.exit_code == 125
    assert "has contract '2'; expected 14" in (result.launch_error or "")
    assert not workspace.exists()
    assert len(calls) == 1
    assert calls[0][1:3] == ("image", "inspect")


def test_startup_marker_is_converted_to_a_bounded_duration(tmp_path) -> None:
    marker = tmp_path / "ready"
    marker.write_text(str(int(101.25 * 1_000_000_000)), encoding="utf-8")
    assert DockerSandbox._startup_seconds(
        marker, wall_started=101.0, duration_seconds=2.0
    ) == pytest.approx(0.25)
    # A corrupt or stale marker must not invent time outside this execution.
    assert DockerSandbox._startup_seconds(marker, wall_started=99.0, duration_seconds=1.0) == 1.0


def test_docker_refusal_is_reported_as_a_launch_error() -> None:
    """125 + a "docker:" prefix means the container never started.

    Surfacing it as an ordinary program failure makes a control model rewrite
    working code until its turn budget is gone.
    """
    error = DockerSandbox._launch_error(
        125,
        "docker: Error response from daemon: NanoCPUs can not be set, as your "
        "kernel does not support CPU CFS scheduler or the cgroup is not mounted\n",
    )
    assert error is not None
    assert "could not be started" in error
    assert "NanoCPUs" in error


def test_program_exiting_125_is_not_mistaken_for_a_docker_refusal() -> None:
    # 125 is only special when docker itself prints it; a program may exit
    # with any code, and its traceback belongs in stderr where the model can
    # act on it.
    assert DockerSandbox._launch_error(125, "Traceback...\nSystemExit: 125\n") is None
    assert DockerSandbox._launch_error(125, "") is None
    assert DockerSandbox._launch_error(1, "docker: irrelevant\n") is None


def test_launch_error_marks_the_result_as_failed() -> None:
    result = SandboxResult(
        exit_code=125,
        stdout="",
        stderr="",
        duration_seconds=0.1,
        launch_error="The sandbox container could not be started: docker: boom",
    )
    assert result.succeeded is False


def test_output_limit_result_defaults_to_false_and_marks_termination_as_failed() -> None:
    succeeded = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1,
    )
    limited = SandboxResult(
        exit_code=0,
        stdout="",
        stderr="",
        duration_seconds=0.1,
        output_limit_exceeded=True,
    )

    assert succeeded.output_limit_exceeded is False
    assert succeeded.succeeded is True
    assert limited.succeeded is False


async def test_cold_sandbox_propagates_output_limit_termination(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def create_process(*_: str, **__: object) -> _CompletedProcess:
        return _CompletedProcess(returncode=-9)

    async def capture_output(*_: object, **__: object) -> BoundedProcessOutput:
        return BoundedProcessOutput(
            stdout=b"x" * 8,
            stderr=b"output limited",
            timed_out=False,
            output_limit_exceeded=True,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("opensac.sandbox.docker.read_bounded_process_output", capture_output)
    socket = tmp_path / "broker.sock"
    socket.touch()
    sandbox = DockerSandbox(
        image="opensac-test",
        broker_socket=socket,
        max_output_bytes=8,
    )
    sandbox._image_contract._verified = True

    result = await sandbox.execute(SandboxRequest("pass", tmp_path / "workspace", "secret"))

    assert result.output_limit_exceeded is True
    assert result.timed_out is False
    assert result.launch_error is None
    assert result.succeeded is False


def test_program_and_output_files_are_named_per_execution(tmp_path) -> None:
    """Two executions sharing a session must not share these two paths.

    They used to be fixed names in the shared workspace, so concurrent calls
    overwrote each other and the container ran whichever program landed last.
    That corrupts the archive as well as the run: the code recorded for a
    sequence would not be the code that produced its result.
    """
    socket = tmp_path / "broker.sock"
    socket.touch()
    sandbox = DockerSandbox(image="opensac-test", broker_socket=socket)
    command = sandbox.command(
        SandboxRequest(
            "pass",
            tmp_path / "workspace",
            "secret",
            program_filename=".opensac-program-007.py",
            output_filename=".opensac-output-007.json",
        )
    )
    assert command[-1] == "/workspace/.opensac-program-007.py"
    assert "OPENSAC_OUTPUT_PATH=/workspace/.opensac-output-007.json" in command


def test_a_syntax_rejection_points_at_the_line_instead_of_naming_it() -> None:
    """A rejection is the whole turn's output, so it is what the retry is written from.

    `(<unknown>, line 3)` is a coordinate into a file the model cannot open: it
    has to work out which line that was from the code it emitted, and it
    reliably rewrites the wrong one. The errors are concentrated enough for
    this to matter -- three quarters of them are quote escaping inside a phrase
    query, where the broken line and its correct neighbour differ by one
    backslash.
    """
    code = 'queries = [\n    "Portland salon owner",\n    \\"Spokane business",\n]\n'
    with pytest.raises(UnsafeCodeError) as caught:
        validate_code(code)

    message = str(caught.value)
    # The original message is kept verbatim; the pointer is added after it.
    assert "Generated code is invalid Python:" in message
    lines = message.splitlines()
    source = next(i for i, line in enumerate(lines) if 'line 3: \\"Spokane business",' in line)
    caret = lines[source + 1]
    assert caret.strip() == "^"
    assert caret.index("^") == lines[source].index('\\"') + 1


def test_the_rejection_that_keeps_happening_is_answered_with_the_line_that_works() -> None:
    """83% of a run's rejections were this one message, and the caret alone did not stop it.

    The pointed-at line is always a phrase query written into a double-quoted
    Python string with the opening quote dropped, because writing it correctly
    starts the line with three quote characters and one goes missing. A caret
    on the backslash invites a fix to the escape. The fix is to stop escaping:
    single quotes carry the phrase with no backslash to lose, so the rejection
    hands back the corrected line rather than describing it.
    """
    code = 'queries = [\n    \\"Urraca\\" of Leon queen regnant book",\n]\n'
    with pytest.raises(UnsafeCodeError) as caught:
        validate_code(code)

    assert "'\"Urraca\" of Leon queen regnant book'" in str(caught.value)


def test_the_hint_stays_out_of_rejections_it_does_not_explain() -> None:
    """A repair suggested for the wrong error is worse than none.

    It is one shape of one message, so it fires on that shape only. The last
    case is the closest neighbour: the same "line continuation character"
    error, from a backslash that has nothing to do with a quoted phrase.
    """
    for code in ('x = "unterminated\n', "def f(:\n    pass\n", "x = 1\n\\y = 2\n"):
        with pytest.raises(UnsafeCodeError) as caught:
            validate_code(code)
        assert "single-quoted" not in str(caught.value)


def test_a_syntax_rejection_survives_a_line_number_it_cannot_use() -> None:
    """The pointer is best-effort; the rejection is not.

    `exc.lineno` can fall outside the source and `exc.offset` can point one
    past the end of its line. Losing the whole rejection to an IndexError
    there would turn a fixable mistake into a silent one, so every malformed
    program must still come back as a rejection that names the problem.
    """
    malformed = [
        "print('unterminated\n",  # offset past the end of the line
        "(",  # one character, never closed
        "\n\n\n    x = (\n",  # error on a line that is only indentation
        "x = 1\n\t y = 2\n",  # mixed indentation
        "�",  # not even decodable as an identifier
    ]
    for code in malformed:
        with pytest.raises(UnsafeCodeError, match="invalid Python"):
            validate_code(code)


async def test_real_subprocess_output_is_bounded_and_terminated() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        ("import os\nwhile True:\n os.write(1, b'x' * 65536)\n os.write(2, b'y' * 65536)\n"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    captured = await read_bounded_process_output(
        process,
        max_output_bytes=8192,
        timeout_seconds=5,
    )

    assert captured.output_limit_exceeded is True
    assert captured.timed_out is False
    assert len(captured.stdout) + len(captured.stderr.split(b"\nOpenSAC", 1)[0]) <= 8192
    assert b"reached the output limit" in captured.stderr
    assert process.returncode != 0


async def test_output_exactly_at_cap_is_not_terminated() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os; os.write(1, b'x' * 4096)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    captured = await read_bounded_process_output(
        process,
        max_output_bytes=4096,
        timeout_seconds=5,
    )

    assert captured.stdout == b"x" * 4096
    assert captured.stderr == b""
    assert captured.output_limit_exceeded is False
    assert process.returncode == 0
