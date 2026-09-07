import httpx
import pytest

from opensac import Client
from opensac.errors import (
    ClientClosedError,
    InvalidRequestError,
    InvalidSchemaError,
    ProviderError,
    ProviderRateLimitError,
    StructuredOutputError,
)
from opensac.provider import ProviderConfig, ProviderContext
from opensac.providers.jina import JinaFetch
from opensac.runtime import Runtime
from opensac.structured import parse_extraction, schema_validator


@pytest.mark.asyncio
async def test_provider_error_type_and_batch_metadata():
    fetch = JinaFetch(
        ProviderConfig(),
        ProviderContext(),
        transport=httpx.MockTransport(lambda request: httpx.Response(429, text="private detail")),
    )
    runtime = Runtime(fetch_provider=fetch)
    try:
        with pytest.raises(ProviderRateLimitError) as caught:
            await runtime.fetch("https://example.com")
        assert isinstance(caught.value, ProviderError)
        assert caught.value.status_code == 429
        assert caught.value.retryable
        batch = await runtime.fetch_many(["https://example.com"])
        assert not batch[0].ok
        assert batch[0].error.code == caught.value.code
        assert batch[0].error.message == str(caught.value)
        assert batch[0].error.retryable
        assert "private detail" not in batch[0].model_dump_json()
    finally:
        await runtime.aclose()


def test_sdk_input_and_lifecycle_errors_are_typed():
    with Client() as client, pytest.raises(InvalidRequestError):
        client.search(" ")
    with pytest.raises(ClientClosedError):
        client.search("query")


def test_schema_and_output_errors_are_distinct():
    with pytest.raises(InvalidSchemaError) as caught:
        schema_validator({"type": "string", "pattern": ".*"})
    assert isinstance(caught.value, InvalidRequestError)
    with pytest.raises(StructuredOutputError) as caught_output:
        parse_extraction('"wrong"', schema_validator({"type": "integer"}))
    assert isinstance(caught_output.value, ProviderError)
