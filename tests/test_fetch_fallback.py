import asyncio
import gzip
from io import BytesIO

import httpx
import pytest
from pydantic import SecretStr
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from opensac.errors import (
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
    ResponseTooLargeError,
)
from opensac.provider import ProviderConfig, ProviderContext
from opensac.providers.jina import JinaFetch
from opensac.runtime import Runtime


def pdf_bytes(*texts: str, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def provider(handle, *, timeout=1, limit=2_000_000):
    return JinaFetch(
        ProviderConfig(api_key=SecretStr("reader-secret")),
        ProviderContext(request_timeout=timeout, max_response_bytes=limit),
        transport=httpx.MockTransport(handle),
    )


@pytest.mark.parametrize(
    "url,media_type,body,expected",
    [
        (
            "https://source.test/data.JSON?download=1",
            "application/json",
            b' {"n": 1}\r\n',
            ' {"n": 1}\r\n',
        ),
        ("https://source.test/data.csv", "text/csv", b'a,b\r\n"x,y",2\r\n', 'a,b\r\n"x,y",2\r\n'),
        (
            "https://source.test/data.csv",
            "text/csv; charset=iso-8859-1",
            b"caf\xe9,2\n",
            "caf\u00e9,2\n",
        ),
        ("https://source.test/data.json", "application/octet-stream", b" \n", " \n"),
    ],
)
async def test_files_bypass_reader_and_preserve_text(url, media_type, body, expected):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "source.test"
        assert "authorization" not in request.headers
        assert "x-return-format" not in request.headers
        return httpx.Response(200, content=body, headers={"Content-Type": media_type})

    fetch = provider(handle)
    try:
        result = await fetch.fetch(url)
        assert result.url == url
        assert result.text == expected
        assert len(calls) == 1
    finally:
        await fetch.aclose()


@pytest.mark.parametrize(
    "failure", ["status", "rate_limit", "timeout", "empty", "encoding", "size"]
)
async def test_reader_failure_falls_back_to_html(failure):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "r.jina.ai":
            if failure == "timeout":
                raise httpx.ReadTimeout("private detail")
            return httpx.Response(
                {"status": 503, "rate_limit": 429}.get(failure, 200),
                content={"empty": b"", "encoding": b"\xff", "size": b"x" * 2049}.get(
                    failure, b"private detail"
                ),
            )
        return httpx.Response(
            200,
            text="<html><body><article><h1>Research</h1><p>Useful paper content.</p></article>"
            "<script>secret_script()</script><style>.hidden{}</style></body></html>",
            headers={"Content-Type": "text/html"},
        )

    fetch = provider(handle, limit=2048)
    try:
        result = await fetch.fetch("https://source.test/article")
        assert "Useful paper content." in result.text
        assert "secret_script" not in result.text
        assert ".hidden" not in result.text
        assert len(calls) == 2
    finally:
        await fetch.aclose()


async def test_stage_timeout_reserves_fallback_time():
    cancelled = asyncio.Event()

    async def handle(request):
        if request.url.host == "r.jina.ai":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        return httpx.Response(200, text="fallback")

    fetch = provider(handle, timeout=0.2)
    try:
        assert (await fetch.fetch("https://source.test/article")).text == "fallback"
        assert cancelled.is_set()
    finally:
        await fetch.aclose()


@pytest.mark.parametrize("url", ["https://source.test/paper.pdf", "https://source.test/download"])
async def test_pdf_signature_and_page_order(url):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "r.jina.ai":
            return httpx.Response(503)
        return httpx.Response(200, content=pdf_bytes("First page", "Second page"))

    fetch = provider(handle)
    try:
        result = await fetch.fetch(url)
        assert result.text.index("First page") < result.text.index("Second page")
        assert "\n\n" in result.text
        assert result.url == url
        assert len(calls) == (1 if url.endswith(".pdf") else 2)
    finally:
        await fetch.aclose()


@pytest.mark.parametrize(
    "body", [b"broken pdf", pdf_bytes(""), pdf_bytes("text", password="secret")]
)
async def test_unreadable_pdf_fails_without_reader(body):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=body, headers={"Content-Type": "application/pdf"})

    fetch = provider(handle)
    try:
        with pytest.raises(ProviderResponseError):
            await fetch.fetch("https://source.test/paper.pdf")
        assert len(calls) == 1
    finally:
        await fetch.aclose()


async def test_redirect_preserves_url_and_content_type_overrides_suffix():
    calls = []

    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers
        if request.url.host == "source.test":
            return httpx.Response(302, headers={"Location": "https://cdn.test/download"})
        return httpx.Response(
            200,
            text="<html><body>HTML instead</body></html>",
            headers={"Content-Type": "text/html"},
        )

    fetch = provider(handle)
    try:
        result = await fetch.fetch("https://source.test/data.json")
        assert result.url == "https://source.test/data.json"
        assert result.text == "HTML instead"
        assert len(calls) == 2
    finally:
        await fetch.aclose()


@pytest.mark.parametrize("location,expected_calls", [("/loop", 6), ("file:///tmp/data", 1)])
async def test_redirect_limits_and_scheme(location, expected_calls):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    fetch = provider(handle)
    try:
        with pytest.raises(ProviderResponseError):
            await fetch.fetch("https://source.test/data.json")
        assert len(calls) == expected_calls
    finally:
        await fetch.aclose()


@pytest.mark.parametrize(
    "body,headers,error",
    [
        (
            gzip.compress(b"x" * 2048),
            {"Content-Encoding": "gzip", "Content-Type": "text/csv"},
            ResponseTooLargeError,
        ),
        ("\u00e9" * 800, {"Content-Type": "text/csv; charset=iso-8859-1"}, ResponseTooLargeError),
        (b"\xff", {"Content-Type": "application/json"}, ProviderResponseError),
        (b"abc", {"Content-Type": "image/png"}, ProviderResponseError),
        (b"<html><body></body></html>", {"Content-Type": "text/html"}, ProviderResponseError),
    ],
)
async def test_download_and_extraction_failures(body, headers, error):
    if isinstance(body, str):
        body = body.encode("iso-8859-1")
    fetch = provider(lambda request: httpx.Response(200, content=body, headers=headers), limit=1024)
    try:
        with pytest.raises(error):
            await fetch.fetch("https://source.test/data.csv")
    finally:
        await fetch.aclose()


async def test_final_error_chains_primary_failure_and_batch_order():
    def handle(request):
        if request.url.host == "r.jina.ai":
            return httpx.Response(429, text="private reader body")
        if request.url.path == "/failed":
            return httpx.Response(503, text="private source body")
        return httpx.Response(200, text=request.url.path)

    closed = []

    class ClosingTransport(httpx.MockTransport):
        async def aclose(self):
            closed.append(self)
            await super().aclose()

    reader_transport = ClosingTransport(handle)
    source_transport = ClosingTransport(handle)
    fetch = JinaFetch(
        ProviderConfig(),
        ProviderContext(),
        transport=reader_transport,
        direct_transport=source_transport,
    )
    async with Runtime(fetch_provider=fetch) as runtime:
        with pytest.raises(ProviderError) as caught:
            await runtime.fetch("https://source.test/failed")
        assert caught.value.code == "provider_error"
        assert caught.value.__cause__.code == "provider_rate_limited"
        items = await runtime.fetch_many(
            ["https://source.test/ok", "https://source.test/failed", "https://source.test/ok"]
        )
        assert [item.ok for item in items] == [True, False, True]
        assert items[0].data == items[2].data
        assert items[1].error.code == "provider_error"
        assert "private" not in items[1].model_dump_json()
    assert closed == [reader_transport, source_transport]


async def test_total_deadline_and_external_cancellation():
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request):
        if request.url.host == "r.jina.ai":
            return httpx.Response(503)
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return httpx.Response(200)

    fetch = provider(handle, timeout=0.1)
    try:
        with pytest.raises(ProviderTimeoutError):
            await fetch.fetch("https://source.test/slow")
        assert cancelled.is_set()
        cancelled.clear()
        entered.clear()
        task = asyncio.create_task(fetch.fetch("https://source.test/slow"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    finally:
        await fetch.aclose()


@pytest.mark.parametrize("media_type", ["application/problem+json", "text/csv"])
async def test_suffix_free_download_uses_content_type(media_type):
    original = ' {"value": 3}\r\n' if media_type.endswith("+json") else "a,b\r\n1,2\r\n"

    def handle(request):
        if request.url.host == "r.jina.ai":
            raise httpx.ConnectError("private detail")
        return httpx.Response(200, content=original.encode(), headers={"Content-Type": media_type})

    fetch = provider(handle)
    try:
        assert (await fetch.fetch("https://source.test/download")).text == original
    finally:
        await fetch.aclose()


async def test_unexpected_reader_failure_does_not_fall_back():
    calls = []

    def handle(request):
        calls.append(request)
        raise RuntimeError("unexpected failure")

    fetch = provider(handle)
    try:
        with pytest.raises(RuntimeError):
            await fetch.fetch("https://source.test/article")
        assert len(calls) == 1
    finally:
        await fetch.aclose()
