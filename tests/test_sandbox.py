from __future__ import annotations

import pytest

from opensac.sandbox import (
    DockerSandbox,
    SandboxRequest,
    SandboxResult,
    UnsafeCodeError,
    validate_code,
)


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
        "    print(sdk.search.__doc__)\n"
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
    assert str(socket.resolve()) in joined
    assert str(workspace.resolve() / ".opensac-container-id") not in joined


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
    assert DockerSandbox._launch_error(125, 'Traceback...\nSystemExit: 125\n') is None
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
    assert 'line 3: \\"Spokane business",' in message
    caret = message.splitlines()[-1]
    assert caret.strip() == "^"
    assert caret.index("^") == message.splitlines()[-2].index('\\"') + 1


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
