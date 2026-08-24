from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensac.config import ConfigurationError, Settings, load_settings

SECRET_ENV_NAMES = {
    "OPENSAC_API_KEY",
    "OPENSAC_MODEL_API_KEY",
    "OPENSAC_SERPER_API_KEY",
    "OPENSAC_JINA_API_KEY",
}


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name in SECRET_ENV_NAMES or name in {
            "OPENSAC_API_HOST",
            "OPENSAC_SEARCH_BACKEND",
            "OPENSAC_SANDBOX_MODE",
        }:
            monkeypatch.delenv(name)


def test_local_template_defines_a_valid_complete_configuration() -> None:
    example = Path(__file__).resolve().parents[1] / "configs/local.yaml"

    settings = load_settings(example)

    assert settings.api_host == "127.0.0.1"
    assert settings.data_dir == example.parent.parent / ".opensac"
    assert settings.search_backend == "local"
    assert settings.provider_operation_concurrency == {}
    assert settings.sandbox_docker_host_platform in {"darwin", "linux"}


@pytest.mark.parametrize("name", ["web.yaml", "web-performance.yaml", "docker.yaml"])
def test_profile_templates_are_valid(name: str) -> None:
    config = Path(__file__).resolve().parents[1] / "configs" / name

    settings = load_settings(config)

    assert settings.search_backend == "web"


def test_partial_yaml_uses_defaults_and_resolves_storage_paths(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    config = deployment / "opensac.yaml"
    config.write_text(
        """
api:
  port: 9000
storage:
  data_dir: state
  broker_socket: run/broker.sock
search:
  backend: web
providers:
  operation_concurrency:
    web.search: 4
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 9000
    assert settings.data_dir == (deployment / "state").resolve()
    assert settings.broker_socket == (deployment / "run/broker.sock").resolve()
    assert settings.search_backend == "web"
    assert settings.provider_operation_concurrency == {"web.search": 4}


def test_no_config_uses_defaults() -> None:
    settings = load_settings()

    assert settings.api_host == "127.0.0.1"
    assert settings.search_backend == "local"


def test_environment_api_keys_override_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    Path(".env").write_text(
        """
OPENSAC_API_KEY=dotenv-public
OPENSAC_MODEL_API_KEY=dotenv-model
OPENSAC_SERPER_API_KEY=dotenv-serper
OPENSAC_JINA_API_KEY=dotenv-jina
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSAC_API_KEY", "environment-public")

    settings = load_settings()

    assert settings.api_key == "environment-public"
    assert settings.model_api_key == "dotenv-model"
    assert settings.serper_api_key == "dotenv-serper"
    assert settings.jina_api_key == "dotenv-jina"


def test_direct_settings_construction_does_not_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSAC_API_KEY", "environment-public")

    settings = Settings(api_host="0.0.0.0")

    assert settings.api_host == "0.0.0.0"
    assert settings.api_key == ""


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("- api\n- storage\n", "must contain a YAML mapping"),
        ("unknown:\n  value: 1\n", "Unknown OpenSAC configuration section"),
        ("api:\n  unknown: 1\n", "Unknown OpenSAC configuration field: api.unknown"),
        ("api:\n  host: one\n  host: two\n", "Duplicate YAML key"),
        ("api:\n  port: invalid\n", "Invalid OpenSAC configuration"),
    ],
)
def test_invalid_yaml_configuration_fails(yaml_text: str, message: str) -> None:
    config = Path("opensac.yaml")
    config.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_settings(config)


def test_empty_yaml_uses_defaults() -> None:
    config = Path("opensac.yaml")
    config.write_text("", encoding="utf-8")

    assert load_settings(config).api_port == 8000


def test_missing_yaml_fails_with_path() -> None:
    config = Path("missing.yaml")

    with pytest.raises(ConfigurationError, match="Cannot read OpenSAC configuration"):
        load_settings(config)


@pytest.mark.parametrize(
    ("yaml_text", "environment_name"),
    [
        ("api:\n  key: secret\n", "OPENSAC_API_KEY"),
        ("model:\n  api_key: secret\n", "OPENSAC_MODEL_API_KEY"),
        ("providers:\n  serper_api_key: secret\n", "OPENSAC_SERPER_API_KEY"),
        ("providers:\n  jina_api_key: secret\n", "OPENSAC_JINA_API_KEY"),
    ],
)
def test_api_keys_are_rejected_in_yaml(yaml_text: str, environment_name: str) -> None:
    config = Path("opensac.yaml")
    config.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=environment_name):
        load_settings(config)


def test_legacy_non_secret_environment_setting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSAC_API_HOST", "0.0.0.0")

    with pytest.raises(ConfigurationError, match="move these settings to YAML: OPENSAC_API_HOST"):
        load_settings()


def test_dotenv_rejects_non_secret_settings() -> None:
    Path(".env").write_text(
        "OPENSAC_API_KEY=secret\nOPENSAC_API_PORT=9000\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="move these settings to YAML: OPENSAC_API_PORT"):
        load_settings()


def test_deployment_files_mount_yaml_and_inject_only_secrets() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    entrypoint = (root / "docker/service-entrypoint.sh").read_text(encoding="utf-8")
    docker_e2e = (root / "tests/test_sandbox_docker_e2e.py").read_text(encoding="utf-8")
    dotenv_names = {
        line.split("=", 1)[0]
        for line in (root / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }

    assert "/etc/opensac/opensac.yaml" in compose
    assert "OPENSAC_CONFIG_FILE" in compose
    assert "- --config" in compose
    assert "OPENSAC_API_HOST" not in compose
    assert "OPENSAC_DATA_DIR" not in entrypoint
    assert "OPENSAC_BROKER_SOCKET" not in entrypoint
    assert "OPENSAC_SANDBOX_DOCKER_HOST_PLATFORM" not in entrypoint
    assert '"OPENSAC_SANDBOX_IMAGE": image' not in docker_e2e
    assert '"build-sandbox",\n                    "--config"' in docker_e2e
    assert dotenv_names == SECRET_ENV_NAMES
