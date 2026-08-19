from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from opensac._contracts import SearchHit
from opensac.broker.call_context import current_call
from opensac.broker.session import BrokerSession
from opensac.models import HitRecord

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "spm",
    }
)
_MAX_SOURCE_CHARS = 4_096
_MAX_ERROR_SOURCE_CHARS = 160


def canonical_url(url: str) -> str:
    """Conservatively fold URL spellings that identify the same page."""

    parts = urlsplit(url.strip())
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    )
    encoded = "&".join(f"{key}={value}" for key, value in query)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, encoded, ""))


def source_for(hit: SearchHit) -> str:
    """Return the source-native public address for a backend hit."""

    if hit.url:
        parts = urlsplit(hit.url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            raise ValueError("Search backend returned a non-HTTP document URL")
        source = canonical_url(hit.url)
    elif hit.docid:
        source = str(hit.docid).strip()
    else:
        raise ValueError("Search backend returned a hit without a URL or docid")
    if not source:
        raise ValueError("Search backend returned an empty document source")
    if len(source) > _MAX_SOURCE_CHARS:
        raise ValueError(
            f"Search backend returned a source longer than {_MAX_SOURCE_CHARS} characters"
        )
    return source


def document_identity(hit: SearchHit) -> str:
    """Return the private backend-scoped identity used by caches and traces."""

    if hit.docid:
        return f"{hit.backend}:docid:{hit.docid}"
    if hit.url:
        return f"{hit.backend}:url:{canonical_url(hit.url)}"
    raise ValueError("Search backend returned a hit without a URL or docid")


def normalize_source(value: Any) -> str:
    source = str(value).strip()
    if len(source) > _MAX_SOURCE_CHARS:
        raise ValueError(f"source must be at most {_MAX_SOURCE_CHARS} characters")
    parts = urlsplit(source)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return canonical_url(source)
    return source


def resolve_sources(state: BrokerSession, sources: list[str]) -> list[SearchHit]:
    """Resolve only sources admitted by search in this live session."""

    resolved = [
        (source, state.documents_by_source.get(normalize_source(source))) for source in sources
    ]
    missing = [source for source, hit in resolved if hit is None]
    if missing:
        rendered = ", ".join(str(source)[:_MAX_ERROR_SOURCE_CHARS] for source in missing[:3])
        raise ValueError(
            f"Unknown sources: {rendered}. Pass a source that search returned in this session."
        )
    hits = [hit for _, hit in resolved if hit is not None]
    context = current_call()
    if context is not None:
        context.hits.extend(
            HitRecord(identity=document_identity(hit), rank=hit.rank, score=hit.score)
            for hit in hits
        )
    return hits
