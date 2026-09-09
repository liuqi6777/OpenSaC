from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, HttpUrl, SecretStr, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSAC_", extra="ignore")
    search_provider: str = "serper"
    search_base_url: HttpUrl | None = None
    search_api_key: SecretStr = SecretStr("")
    search_options: dict[str, Any] = Field(default_factory=dict)
    fetch_provider: str = "jina"
    fetch_base_url: HttpUrl | None = None
    fetch_api_key: SecretStr = SecretStr("")
    fetch_options: dict[str, Any] = Field(default_factory=dict)
    rerank_provider: str = "http"
    rerank_base_url: HttpUrl | None = None
    rerank_api_key: SecretStr = SecretStr("")
    rerank_model: ModelName | None = None
    rerank_options: dict[str, Any] = Field(default_factory=dict)
    llm_provider: str = "openai"
    llm_base_url: HttpUrl | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_model: ModelName | None = None
    llm_max_tokens: int = Field(default=1024, ge=1, le=65536)
    llm_temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    llm_options: dict[str, Any] = Field(default_factory=dict)
    request_timeout: float = Field(default=30, gt=0, le=300)
    max_concurrency: int = Field(default=8, ge=1, le=128)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    trace_dir: Path | None = None
    trace_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    trace_action_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def provider_endpoints(self) -> Self:
        for url in (
            self.search_base_url,
            self.fetch_base_url,
            self.rerank_base_url,
            self.llm_base_url,
        ):
            if url is not None and (url.username or url.password or url.query or url.fragment):
                raise ValueError("Base URLs cannot contain credentials, query or fragment")
        return self
