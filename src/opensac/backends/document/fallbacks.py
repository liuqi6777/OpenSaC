"""Bounded alternate representations for document providers."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

from opensac._contracts import SearchHit


def document_fetch_candidates(hit: SearchHit) -> list[SearchHit]:
    """Return the original target followed by safe, provider-agnostic fallbacks."""

    candidates = [hit]
    fallback_url = _internet_archive_text_url(hit.url)
    if fallback_url is None or fallback_url == hit.url:
        return candidates
    fallback = hit.model_copy(deep=True)
    fallback.url = fallback_url
    fallback.metadata["_opensac_representation"] = "internet_archive_djvu_text"
    candidates.append(fallback)
    return candidates


def _internet_archive_text_url(url: str | None) -> str | None:
    parts = urlsplit(str(url or ""))
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if (parts.hostname or "").rstrip(".").lower() not in {"archive.org", "www.archive.org"}:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0] not in {"details", "download"}:
        return None
    identifier = unquote(segments[1]).strip()
    if not identifier or identifier in {".", ".."} or "/" in identifier or "\\" in identifier:
        return None
    encoded = quote(identifier, safe="")
    return f"https://archive.org/download/{encoded}/{encoded}_djvu.txt"
