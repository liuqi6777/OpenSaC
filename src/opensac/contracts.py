from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from .errors import ErrorInfo

_HTTP_URL = TypeAdapter(HttpUrl)


def validate_url(value: str) -> str:
    _HTTP_URL.validate_python(value)
    return value


URLString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8192),
    AfterValidator(validate_url),
]


class SearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: URLString
    title: str
    snippet: str
    domain: str | None = None
    date: str | None = None

    @model_validator(mode="after")
    def default_domain(self) -> Self:
        if self.domain is None:
            object.__setattr__(self, "domain", _HTTP_URL.validate_python(self.url).host)
        return self


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)
    url: URLString
    text: str


class BatchItem[T](BaseModel):
    data: T | None = None
    error: ErrorInfo | None = None

    @property
    def ok(self) -> bool:
        """Whether this item succeeded, including empty results."""
        return self.error is None

    @model_validator(mode="after")
    def exactly_one_outcome(self) -> Self:
        if (self.data is None) == (self.error is None):
            raise ValueError("Expected exactly one of data/error")
        return self


class Capabilities(BaseModel):
    methods: list[str]
    limits: dict[str, Any]


class RerankResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    index: int = Field(ge=0, strict=True)
    relevance_score: float = Field(allow_inf_nan=False)


class TokenUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class Completion(BaseModel):
    text: str
    model: str
    usage: TokenUsage | None = None


class Extraction(BaseModel):
    data: Any
    model: str
    usage: TokenUsage | None = None
