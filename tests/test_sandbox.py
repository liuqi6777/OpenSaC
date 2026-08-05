from __future__ import annotations

import pytest

from opensac.sandbox import DockerSandbox, SandboxRequest, UnsafeCodeError, validate_code


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
