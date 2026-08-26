from __future__ import annotations

import pytest

from opensac import _optional
from opensac.agent.mcp import create_server
from opensac.api.runtime import ApplicationRuntime
from opensac.config import Settings
from sac_agent.client import ModelClient, ModelConfig


def _unavailable(_: str):
    return None


def test_missing_extra_error_names_feature_modules_and_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_optional, "find_spec", _unavailable)

    with pytest.raises(_optional.MissingOptionalDependency) as raised:
        _optional.require_extra("Pipeline LLM support", "llm", ("jsonschema", "openai"))

    message = str(raised.value)
    assert "Pipeline LLM support" in message
    assert "jsonschema, openai" in message
    assert "opensac[llm]" in message


def test_base_runtime_does_not_require_pipeline_llm_dependencies(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_optional, "find_spec", _unavailable)

    runtime = ApplicationRuntime(
        Settings(
            data_dir=tmp_path / "data",
            broker_socket=tmp_path / "broker.sock",
        )
    )

    assert runtime.broker.llm.service is None
    assert runtime.broker.llm_service is None


def test_configured_pipeline_model_requires_llm_extra(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_optional, "find_spec", _unavailable)

    with pytest.raises(_optional.MissingOptionalDependency, match=r"opensac\[llm\]"):
        ApplicationRuntime(
            Settings(
                data_dir=tmp_path / "data",
                broker_socket=tmp_path / "broker.sock",
                backends={
                    "llm": {
                        "provider": "openai_compatible",
                        "model": "pipeline-model",
                    }
                },
            )
        )


def test_mcp_server_requires_mcp_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_optional, "find_spec", _unavailable)

    with pytest.raises(_optional.MissingOptionalDependency, match=r"opensac\[mcp\]"):
        create_server()


def test_control_agent_requires_agent_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_optional, "find_spec", _unavailable)

    with pytest.raises(_optional.MissingOptionalDependency, match=r"opensac\[agent\]"):
        ModelClient(ModelConfig(model="control-model"))
