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
    assert settings.backend_name == "local"
    assert settings.backends.search.provider == "local"
    assert settings.backends.document.provider == "local"
    assert settings.backends.rerank.provider == "lexical"
    assert settings.backends.llm.provider == "none"
    assert settings.capabilities.search.max_top_k == 600
    assert settings.capabilities.extraction.max_repair_attempts == 1
    assert settings.provider_services.search.concurrency is None
    assert settings.provider_services.llm.concurrency is None
    assert settings.sandbox_docker_host_platform in {"darwin", "linux"}


@pytest.mark.parametrize("name", ["web.yaml", "web-performance.yaml", "docker.yaml"])
def test_profile_templates_are_valid(name: str) -> None:
    config = Path(__file__).resolve().parents[1] / "configs" / name

    settings = load_settings(config)

    assert settings.backend_name == "web"
    assert settings.backends.search.provider == "serper"
    assert settings.backends.document.provider == "jina"


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
backends:
  search:
    provider: serper
  document:
    provider: jina
capabilities:
  search:
    max_top_k: 42
providers:
  services:
    search:
      concurrency: 4
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 9000
    assert settings.data_dir == (deployment / "state").resolve()
    assert settings.broker_socket == (deployment / "run/broker.sock").resolve()
    assert settings.backend_name == "web"
    assert settings.capabilities.search.max_top_k == 42
    assert settings.provider_services.search.concurrency == 4


def test_web_backends_accept_custom_base_urls() -> None:
    settings = Settings(
        backends={
            "search": {
                "provider": "serper",
                "base_url": "https://search.example.test/api",
            },
            "document": {
                "provider": "jina",
                "base_url": "https://reader.example.test",
            },
        }
    )

    assert settings.backends.search.base_url == "https://search.example.test/api"
    assert settings.backends.document.base_url == "https://reader.example.test"


def test_no_config_uses_defaults() -> None:
    settings = load_settings()

    assert settings.api_host == "127.0.0.1"
    assert settings.backend_name == "local"
    assert settings.backends.search.base_url == "http://127.0.0.1:8081"
    assert settings.backends.document.base_url == "http://127.0.0.1:8081"
    assert settings.dashboard_is_enabled is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_dashboard_defaults_to_enabled_only_on_loopback(host: str) -> None:
    assert Settings(api_host=host).dashboard_is_enabled is True


def test_dashboard_remote_exposure_requires_explicit_enable_and_api_key() -> None:
    assert Settings(api_host="0.0.0.0").dashboard_is_enabled is False
    with pytest.raises(ValueError, match="requires OPENSAC_API_KEY"):
        Settings(api_host="0.0.0.0", dashboard_enabled=True)

    settings = Settings(
        api_host="0.0.0.0",
        dashboard_enabled=True,
        api_key="configured-secret",
    )
    assert settings.dashboard_is_enabled is True


def test_dashboard_yaml_can_disable_loopback_default(tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text("dashboard:\n  enabled: false\n", encoding="utf-8")

    assert load_settings(config).dashboard_is_enabled is False


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
        (
            "backends:\n  search:\n    unknown: 1\n",
            "Unknown OpenSAC configuration field: backends.search.unknown",
        ),
        (
            "backends:\n  unknown:\n    provider: local\n",
            "Unknown OpenSAC configuration backend: backends.unknown",
        ),
        (
            "capabilities:\n  unknown:\n    value: 1\n",
            "Unknown OpenSAC configuration capability: capabilities.unknown",
        ),
        (
            "capabilities:\n  search:\n    unknown: 1\n",
            "Unknown OpenSAC configuration field: capabilities.search.unknown",
        ),
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
        ("backends:\n  llm:\n    api_key: secret\n", "OPENSAC_MODEL_API_KEY"),
        ("backends:\n  search:\n    api_key: secret\n", "OPENSAC_SERPER_API_KEY"),
        ("backends:\n  document:\n    api_key: secret\n", "OPENSAC_JINA_API_KEY"),
        ("backends:\n  rerank:\n    api_key: secret\n", "OPENSAC_JINA_API_KEY"),
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


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("model:\n  name: pipeline-model\n", "Unknown OpenSAC configuration section"),
        ("search:\n  max_top_k: 10\n", "Unknown OpenSAC configuration section"),
        ("content:\n  passage_ranker: lexical\n", "Unknown OpenSAC configuration section"),
        ("extraction:\n  max_items: 10\n", "Unknown OpenSAC configuration section"),
    ],
)
def test_legacy_yaml_backend_schema_is_rejected(yaml_text: str, message: str) -> None:
    config = Path("opensac.yaml")
    config.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_settings(config)


def test_backend_provider_pair_is_validated() -> None:
    with pytest.raises(ValueError, match=r"local \+ local or serper \+ jina"):
        Settings(
            backends={
                "search": {"provider": "serper"},
                "document": {"provider": "local"},
            }
        )


def test_llm_backend_requires_an_explicit_model() -> None:
    with pytest.raises(ValueError, match="backends.llm.model is required"):
        Settings(backends={"llm": {"provider": "openai_compatible"}})


def test_rerank_backend_requires_a_model_only_for_jina() -> None:
    with pytest.raises(ValueError, match="backends.rerank.model is required"):
        Settings(backends={"rerank": {"provider": "jina"}})
    with pytest.raises(ValueError, match="supported only by the jina provider"):
        Settings(backends={"rerank": {"provider": "lexical", "model": "rerank-model"}})
    with pytest.raises(ValueError, match="literal_error"):
        Settings(backends={"rerank": {"provider": "none"}})

    settings = Settings(backends={"rerank": {"provider": "jina", "model": "rerank-model"}})
    assert settings.backends.rerank.provider == "jina"
    assert settings.backends.rerank.model == "rerank-model"


@pytest.mark.parametrize(
    "field",
    [
        "operation_concurrency",
        "operation_requests_per_second",
        "operation_burst",
        "operation_attempt_timeout_seconds",
        "operation_logical_deadline_seconds",
    ],
)
def test_old_provider_operation_yaml_fields_are_rejected(field: str, tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text(f"providers:\n  {field}: {{}}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match=f"providers.{field}"):
        load_settings(config)


def test_provider_services_reject_unknown_service_names() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        Settings(provider_services={"custom": {"concurrency": 1}})


def test_rerank_service_policy_applies_to_the_default_lexical_backend() -> None:
    settings = Settings(provider_services={"rerank": {"concurrency": 1}})

    assert settings.provider_services.rerank.concurrency == 1


def test_llm_service_policy_requires_an_enabled_backend() -> None:
    with pytest.raises(ValueError, match="requires an enabled LLM provider"):
        Settings(provider_services={"llm": {"concurrency": 1}})

    settings = Settings(
        backends={"llm": {"provider": "openai_compatible", "model": "pipeline-model"}},
        provider_services={"llm": {"concurrency": 1}},
    )
    assert settings.provider_services.llm.concurrency == 1


@pytest.mark.parametrize(
    "field",
    ["passage_ranker", "passage_reranker_model", "passage_ranking"],
)
def test_old_passage_reranker_yaml_fields_are_rejected(field: str, tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text(
        f"capabilities:\n  content:\n    {field}: legacy\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=f"capabilities.content.{field}"):
        load_settings(config)


def test_llm_backend_yaml_loads_non_secret_connection_settings(tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text(
        """
backends:
  llm:
    provider: openai_compatible
    model: pipeline-model
    base_url: https://llm.example.test/v1
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.backends.llm.provider == "openai_compatible"
    assert settings.backends.llm.model == "pipeline-model"
    assert settings.backends.llm.base_url == "https://llm.example.test/v1"


def test_capability_yaml_loads_nested_policy_settings(tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text(
        """
capabilities:
  search:
    max_queries_per_request: 7
  content:
    url_admission: searched_only
  extraction:
    max_repair_attempts: 2
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.capabilities.search.max_queries_per_request == 7
    assert settings.capabilities.content.url_admission == "searched_only"
    assert settings.capabilities.extraction.max_repair_attempts == 2


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
