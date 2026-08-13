"""Server-side shaping for local-search result snippets."""

from __future__ import annotations

import re
from collections import Counter

try:
    import tiktoken
except ModuleNotFoundError:
    tiktoken = None


SUPPORTED_RESULT_MODES = frozenset({"full", "compact", "query_aware"})

_SNIPPET_ENCODING = None
_FRONTMATTER_PATTERN = re.compile(
    r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL
)
_TERM_PATTERN = re.compile(
    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u9fff]"
)
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
    }
)


def _get_snippet_encoding():
    global _SNIPPET_ENCODING
    if tiktoken is None:
        raise RuntimeError(
            "tiktoken is required to truncate local search snippets by token count."
        )
    if _SNIPPET_ENCODING is None:
        _SNIPPET_ENCODING = tiktoken.get_encoding("cl100k_base")
    return _SNIPPET_ENCODING


def _truncate_head(
    text: str, max_tokens: int, *, append_ellipsis: bool = False
) -> str:
    if not text:
        return text
    encoding = _get_snippet_encoding()
    token_ids = encoding.encode(text)
    if len(token_ids) <= max_tokens:
        return text
    truncated = encoding.decode(token_ids[:max_tokens]).rstrip()
    return truncated + ("..." if append_ellipsis else "")


def _parse_document_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        key = key.strip().lower()
        if separator and key and key not in fields:
            fields[key] = value.strip()
    return fields, text[match.end() :]


def _document_fields(text: str) -> tuple[str, str, str]:
    fields, body = _parse_document_frontmatter(text)
    title = fields.get("title", "").strip()
    date = fields.get("date", "").strip()
    body = body.strip()
    if title:
        first_line, separator, remainder = body.partition("\n")
        if first_line.strip() == title:
            body = remainder.strip() if separator else ""
    return title, date, " ".join(body.split())


def _lexical_terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TERM_PATTERN.finditer(text)]


def _query_terms(query: str) -> set[str]:
    raw_terms = _lexical_terms(query)
    filtered = {term for term in raw_terms if term not in _QUERY_STOPWORDS}
    return filtered or set(raw_terms)


def _window_score(
    window: str, query_terms: set[str], normalized_query: str
) -> tuple[int, int, int, int]:
    counts = Counter(_lexical_terms(window))
    matched = {term for term in query_terms if counts.get(term, 0)}
    phrase_match = int(bool(normalized_query and normalized_query in window.lower()))
    coverage = len(matched)
    specificity = sum(min(len(term), 12) for term in matched)
    occurrences = sum(min(counts[term], 3) for term in matched)
    return phrase_match, coverage, specificity, occurrences


def _query_aware_snippet(text: str, query: str, max_tokens: int) -> str:
    if not text:
        return text
    encoding = _get_snippet_encoding()
    token_ids = encoding.encode(text)
    if len(token_ids) <= max_tokens:
        return text

    last_start = len(token_ids) - max_tokens
    stride = max(1, max_tokens // 2)
    starts = list(range(0, last_start + 1, stride))
    if starts[-1] != last_start:
        starts.append(last_start)

    query_terms = _query_terms(query)
    normalized_query = " ".join(query.lower().split())
    best_start = 0
    best_score = (0, 0, 0, 0)
    for start in starts:
        window = encoding.decode(token_ids[start : start + max_tokens])
        score = _window_score(window, query_terms, normalized_query)
        if score > best_score:
            best_start = start
            best_score = score

    end = best_start + max_tokens
    snippet = encoding.decode(token_ids[best_start:end]).strip()
    if best_start > 0:
        snippet = "... " + snippet
    if end < len(token_ids):
        snippet += "..."
    return snippet


def build_snippet_payload(
    *,
    query: str,
    text: str,
    mode: str,
    snippet_max_tokens: int,
    compact_snippet_tokens: int,
    query_aware_snippet_tokens: int,
) -> dict[str, str]:
    """Return exactly the structured fields exposed by a search hit."""
    if mode == "full":
        return {"snippet": _truncate_head(text, snippet_max_tokens)}

    title, date, body = _document_fields(text)
    if mode == "compact":
        snippet = _truncate_head(body, compact_snippet_tokens, append_ellipsis=True)
    elif mode == "query_aware":
        snippet = _query_aware_snippet(body, query, query_aware_snippet_tokens)
    else:
        raise ValueError(f"Unsupported local search result mode: {mode}")
    return {"title": title, "date": date, "snippet": snippet}
