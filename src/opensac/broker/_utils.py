"""Stateless helpers shared by broker capabilities."""

from __future__ import annotations

import ipaddress
import math
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from opensac.backends.document import DocumentHandle
from opensac.backends.search import SearchHit

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


def document_handle_for_hit(hit: SearchHit, *, source: str) -> DocumentHandle:
    """Copy one search result into the document backend's input contract."""

    return DocumentHandle(
        source=source,
        url=hit.url,
        docid=hit.docid,
        title=hit.title,
        date=hit.date,
        metadata=dict(hit.metadata),
    )


def document_identity(route: str, handle: DocumentHandle) -> str:
    """Return the private backend-scoped identity used by caches and traces."""

    if handle.docid:
        return f"{route}:docid:{handle.docid}"
    if handle.url:
        return f"{route}:url:{canonical_url(handle.url)}"
    raise ValueError("Document handle has neither a URL nor a document identifier")


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


def string(
    value: Any,
    name: str,
    *,
    strip: bool = False,
    nonempty: bool = True,
    max_chars: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip() if strip else value
    if nonempty and not normalized.strip():
        raise ValueError(f"{name} must not be empty")
    if max_chars is not None and len(normalized) > max_chars:
        raise ValueError(f"{name} has {len(normalized)} characters; must be at most {max_chars}")
    return normalized


def optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return string(value, name, nonempty=False)


def integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def optional_integer(
    value: Any,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    return integer(value, name, minimum=minimum, maximum=maximum)


def finite_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be a finite number")
    if not minimum <= normalized <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return normalized


def _string_list(
    value: Any,
    name: str,
    *,
    max_items: int | None = None,
    max_item_chars: int | None = None,
    strip: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of strings")
    if max_items is not None and len(value) > max_items:
        raise ValueError(f"{name} must contain at most {max_items} items")
    return [
        string(
            item,
            f"{name}[{index}]",
            strip=strip,
            max_chars=max_item_chars,
        )
        for index, item in enumerate(value)
    ]


def optional_string_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    return _string_list(value, name)
