from __future__ import annotations

import ipaddress
from copy import deepcopy
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

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
    query_parts = []
    for item in parts.query.split("&") if parts.query else []:
        raw_key = item.partition("=")[0]
        if unquote_plus(raw_key).lower() not in _TRACKING_PARAMS:
            query_parts.append(item)
    query = "&".join(sorted(query_parts))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def public_web_url(value: Any) -> str:
    """Validate and normalize a bounded public HTTP(S) document address."""

    source = normalize_web_source(value)
    if any(ord(character) < 32 for character in source):
        raise ValueError("URL contains control characters")
    parts = urlsplit(source)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("source is not an absolute HTTP or HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL userinfo is not allowed")
    try:
        hostname = parts.hostname or ""
        _ = parts.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    lowered = hostname.rstrip(".").lower()
    if not lowered or lowered == "localhost" or lowered.endswith((".localhost", ".local")):
        raise ValueError("URL host is not public")
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("URL IP address is not public")
    return source


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
    if not isinstance(value, str):
        raise ValueError("source must be a string")
    source = value.strip()
    if not source:
        raise ValueError("source must not be empty")
    if len(source) > _MAX_SOURCE_CHARS:
        raise ValueError(f"source must be at most {_MAX_SOURCE_CHARS} characters")
    parts = urlsplit(source)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return canonical_url(source)
    return source


def normalize_web_source(value: Any) -> str:
    """Normalize a web source, inferring HTTPS for an unambiguous bare host."""

    source = normalize_source(value)
    parts = urlsplit(source)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return source
    if "://" in source:
        return source

    candidate = f"https:{source}" if source.startswith("//") else f"https://{source}"
    candidate_parts = urlsplit(candidate)
    try:
        hostname = candidate_parts.hostname or ""
        _ = candidate_parts.port
    except ValueError:
        return source
    lowered = hostname.rstrip(".").lower()
    if not lowered:
        return source
    try:
        ipaddress.ip_address(lowered)
    except ValueError:
        looks_like_host = "." in lowered or lowered == "localhost"
    else:
        looks_like_host = True
    return canonical_url(candidate) if looks_like_host else source


def resolve_sources(state: BrokerSession, sources: list[str]) -> list[SearchHit]:
    """Resolve only sources admitted by search in this live session."""

    resolved = [(source, state.document_for_alias(normalize_source(source))) for source in sources]
    missing = [source for source, record in resolved if record is None]
    if missing:
        rendered = ", ".join(str(source)[:_MAX_ERROR_SOURCE_CHARS] for source in missing[:3])
        raise ValueError(
            f"Unknown sources: {rendered}. Pass a source that search returned in this session."
        )
    hits: list[SearchHit] = []
    for requested_source, record in resolved:
        if record is None:
            continue
        hit = deepcopy(record.hit)
        hit.source = requested_source.strip()
        hits.append(hit)
    context = current_call()
    if context is not None:
        context.hits.extend(
            HitRecord(
                identity=document_identity(hit),
                rank=hit.rank,
                score=hit.score,
                admission=record.admission,
            )
            for hit, (_requested_source, record) in zip(hits, resolved, strict=True)
            if record is not None
        )
    return hits
