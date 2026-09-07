import pytest
from conftest import FakeProvider

from opensac import Client, Document, OpenSACError


def test_search_fetch_without_session_and_save_locally(sdk_client: Client, tmp_path) -> None:
    hits = sdk_client.search("research")
    document = sdk_client.content.fetch(hits[0].url)
    artifact = tmp_path / "paper.json"
    artifact.write_text(document.model_dump_json(), encoding="utf-8")
    restored = Document.model_validate_json(artifact.read_text())
    assert restored.url == "https://example.com/paper"
    assert restored.text == "Full document"
    assert restored.model_dump() == {"url": "https://example.com/paper", "text": "Full document"}


def test_fetch_without_search(sdk_client: Client, provider: FakeProvider) -> None:
    result = sdk_client.content.fetch("https://example.com/unseen")
    assert result.text == "Full document"
    assert provider.calls == ["https://example.com/unseen"]


def test_fetch_requires_a_url_not_an_opaque_identifier(sdk_client: Client, provider: FakeProvider):
    with pytest.raises(OpenSACError) as caught:
        sdk_client.content.fetch("paper-123")
    assert caught.value.code == "invalid_request"
    assert provider.calls == []


def test_empty_search_is_success(sdk_client: Client) -> None:
    assert sdk_client.search("empty") == []


def test_batch_aligns_partial_failures_and_duplicates(sdk_client: Client) -> None:
    urls = ["https://example.com/a", "https://example.com/failed", "https://example.com/a"]
    results = sdk_client.content.fetch_many(urls)
    assert len(results) == 3
    assert [r.ok for r in results] == [True, False, True]
    assert results[0].data.url == urls[0]
    assert results[1].data is None
    assert results[1].error.code == "provider_timeout"
    assert results[1].error.retryable
    assert results[2].data.url == urls[2]


def test_search_many_includes_empty_success(sdk_client: Client) -> None:
    results = sdk_client.search.many(["first", "empty", "last"])
    assert results[0].data[0].title == "first"
    assert results[1].data == []
    assert results[1].ok
    assert results[1].error is None
    assert results[2].data[0].title == "last"


def test_search_reformulations_return_one_deduplicated_result_list(
    sdk_client: Client, provider: FakeProvider
) -> None:
    hits = sdk_client.search(["primary", "alternate", "primary"], limit=2)
    assert len(hits) == 1
    assert hits[0].title == "primary"
    assert provider.calls == ["primary", "alternate"]


def test_provider_error_is_structured(sdk_client: Client) -> None:
    with pytest.raises(OpenSACError) as caught:
        sdk_client.content.fetch("https://example.com/failed")
    assert caught.value.code == "provider_timeout"
    assert caught.value.status_code == 504


@pytest.mark.parametrize("query,limit", [(" ", 5), ("ok", 0), ("ok", 101), ("ok", True)])
def test_invalid_search_does_not_reach_provider(
    sdk_client: Client, provider: FakeProvider, query, limit
):
    with pytest.raises(OpenSACError) as caught:
        sdk_client.search(query, limit=limit)
    assert caught.value.code == "invalid_request"
    assert provider.calls == []


@pytest.mark.parametrize("urls", [[], ["https://example.com"] * 33])
def test_batch_bounds(sdk_client: Client, urls) -> None:
    with pytest.raises(OpenSACError) as caught:
        sdk_client.content.fetch_many(urls)
    assert caught.value.status_code == 422


@pytest.mark.parametrize("url", ["file:///etc/passwd", "not-a-url", "https://"])
def test_invalid_url_format_does_not_reach_provider(
    sdk_client: Client, provider: FakeProvider, url
):
    with pytest.raises(OpenSACError) as caught:
        sdk_client.content.fetch(url)
    assert caught.value.code == "invalid_request"
    assert provider.calls == []


@pytest.mark.parametrize("url", ["http://localhost:9000/paper", "http://127.0.0.1/private"])
def test_target_acceptance_belongs_to_provider(sdk_client: Client, provider: FakeProvider, url):
    assert sdk_client.content.fetch(url).url == url
    assert provider.calls == [url]


def test_capabilities(sdk_client: Client) -> None:
    result = sdk_client.capabilities()
    assert result.limits["batch_size"] == 32
    assert result.limits["search_reformulations"] == 10


def test_error_input_not_reflected(sdk_client):
    with pytest.raises(OpenSACError) as caught:
        sdk_client.search({"secret": "do-not-reflect"})
    assert caught.value.code == "invalid_request"
    assert "do-not-reflect" not in str(caught.value)


@pytest.mark.parametrize(
    "url,domain",
    [
        ("https://news.example.com/article", "news.example.com"),
        ("http://localhost:9000/paper", "localhost"),
        ("http://127.0.0.1:9000/paper", "127.0.0.1"),
    ],
)
def test_search_metadata_defaults_and_roundtrip(url, domain):
    from opensac import SearchHit

    hit = SearchHit(url=url, title="Title", snippet="Summary")
    assert hit.domain == domain
    assert hit.date is None
    assert SearchHit.model_validate_json(hit.model_dump_json()) == hit
