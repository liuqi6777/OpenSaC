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
    validate_code("from opensac_sdk import sdk\nhits = sdk.search.web('query')")


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess",
        "from socket import socket",
        "eval('1 + 1')",
        "object.__subclasses__()",
    ],
)
def test_validator_blocks_dangerous_code(code) -> None:
    with pytest.raises(UnsafeCodeError):
        validate_code(code)


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
