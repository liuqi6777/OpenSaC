"""Bounded source downloads and local document extraction for Jina fallback."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader
from pypdf.errors import DependencyError, PyPdfError
from trafilatura import extract, html2txt

from ..contracts import Document
from ..errors import (
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ResponseTooLargeError,
)
from ..provider import ProviderContext

_FILE_SUFFIXES = {".pdf", ".json", ".csv"}


def file_suffix(url: str) -> str:
    return PurePosixPath(urlsplit(url).path).suffix.lower()


def is_file_url(url: str) -> bool:
    return file_suffix(url) in _FILE_SUFFIXES


def _bounded_text(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) > limit:
        raise ResponseTooLargeError("Extracted document exceeds limit.")
    return text


def _pdf_text(body: bytes, limit: int) -> str:
    try:
        reader = PdfReader(BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ProviderResponseError("PDF requires a password.")
        pages: list[str] = []
        size = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            size += len(text.encode("utf-8")) + (2 if pages else 0)
            if size > limit:
                raise ResponseTooLargeError("Extracted document exceeds limit.")
            pages.append(text)
    except (
        PyPdfError,
        DependencyError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OSError,
        RecursionError,
    ) as exc:
        raise ProviderResponseError("PDF could not be read.") from exc
    text = "\n\n".join(pages)
    if not text.strip():
        raise ProviderResponseError("PDF contains no extractable text.")
    return text


def _extract_document(response: httpx.Response, body: bytes, limit: int) -> str:
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    suffix = file_suffix(str(response.url))
    # A PDF signature overrides metadata; a concrete media type overrides the suffix.
    if body[:1024].lstrip().startswith(b"%PDF-") or media_type == "application/pdf":
        return _pdf_text(body, limit)
    generic = media_type in {"", "application/octet-stream", "binary/octet-stream"}
    if generic and suffix == ".pdf":
        return _pdf_text(body, limit)
    is_html = media_type in {"text/html", "application/xhtml+xml"}
    is_text = (
        media_type.startswith("text/")
        or media_type in {"application/json", "application/csv", "application/xml"}
        or media_type.endswith(("+json", "+xml"))
        or (generic and suffix in {".json", ".csv"})
    )
    if generic and not is_text:
        is_html = body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
    if not is_html and not is_text:
        raise ProviderResponseError("Source returned unsupported binary content.")
    try:
        text = body.decode(response.charset_encoding or "utf-8")
    except (UnicodeError, LookupError) as exc:
        raise ProviderResponseError("Source returned invalid text encoding.") from exc
    if is_html:
        try:
            text = extract(text, output_format="txt", include_comments=False) or html2txt(text)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ProviderResponseError("HTML could not be read.") from exc
        if not text.strip():
            raise ProviderResponseError("HTML contains no extractable text.")
    return _bounded_text(text, limit)


class DirectFetch:
    def __init__(
        self,
        context: ProviderContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.context = context
        self._http = httpx.AsyncClient(
            timeout=context.request_timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self, url: str) -> Document:
        target = httpx.URL(url)
        try:
            for redirects in range(6):
                async with self._http.stream("GET", target) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirects == 5:
                            raise ProviderResponseError(
                                "Source redirect limit or location invalid."
                            )
                        target = response.url.join(location)
                        if target.scheme not in {"http", "https"}:
                            raise ProviderResponseError("Source redirect requires HTTP(S).")
                        continue
                    if response.status_code == 429:
                        raise ProviderRateLimitError("Source rate limited.")
                    if not response.is_success:
                        raise ProviderError(
                            "Source request failed.", retryable=response.status_code >= 500
                        )
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(chunks) + len(chunk) > self.context.max_response_bytes:
                            raise ResponseTooLargeError("Source response exceeds limit.")
                        chunks.extend(chunk)
                    text = await asyncio.to_thread(
                        _extract_document, response, bytes(chunks), self.context.max_response_bytes
                    )
                    return Document(url=url, text=text)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Source request timed out.") from exc
        except httpx.DecodingError as exc:
            raise ProviderResponseError("Source returned invalid encoded data.") from exc
        except (httpx.TransportError, httpx.InvalidURL) as exc:
            raise ProviderUnavailableError("Source connection failed.") from exc
        raise ProviderResponseError("Source redirect limit exceeded.")
