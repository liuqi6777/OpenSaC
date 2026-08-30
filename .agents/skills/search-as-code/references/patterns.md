# Search-as-Code patterns

This optional helper implements a single stdout budget across fan-out rows. It is a projection
utility, not a research pipeline or completion protocol.

## Emit one globally bounded observation

Use one emitter when a checkpoint can produce more than a few candidate, unit, or evidence rows.
Collect and normalize rows during capability handling; do not print from those loops. The emitter
keeps the source on every shown row, preserves room for counts, and makes omission explicit. Adapt
the row fields and limit, but keep one budget over every code path. `key` names the material
requirement or unit; keep `source` separate instead of constructing `source::field` keys.

```python
def one_line(value):
    return " ".join(str(value or "").split())


def emit_observation(rows, *, max_chars=3_800):
    primary_by_key = {}
    secondary = []
    seen = set()
    for row in rows:
        normalized = {
            "key": one_line(row.get("key")),
            "status": one_line(row.get("status")) or "unknown",
            "source": one_line(row.get("source")),
            "excerpt": one_line(row.get("excerpt"))[:180],
        }
        identity = tuple(normalized.values())
        if identity not in seen:
            seen.add(identity)
            if normalized["key"] not in primary_by_key:
                primary_by_key[normalized["key"]] = normalized
            else:
                secondary.append(normalized)

    # Shrink primary excerpts until every material key fits, then spend residual budget on extras.
    primary = list(primary_by_key.values())
    unique = [*primary, *secondary]
    failures = sum(row["status"] == "failed" for row in unique)

    def render(row, excerpt_chars):
        return (
            f"ROW key={row['key']!r} status={row['status']} "
            f"source={row['source']!r} excerpt={row['excerpt'][:excerpt_chars]!r}"
        )

    excerpt_chars = 180
    while True:
        primary_lines = [render(row, excerpt_chars) for row in primary]
        footer = (
            f"COUNTS total={len(unique)} shown={len(primary_lines)} "
            f"omitted={len(unique) - len(primary_lines)} failures={failures}"
        )
        if len("\n".join([*primary_lines, footer])) <= max_chars or excerpt_chars == 0:
            break
        excerpt_chars = max(0, excerpt_chars - 20)

    shown_lines = []
    for row in unique:
        line = render(row, excerpt_chars)
        next_shown = len(shown_lines) + 1
        footer = (
            f"COUNTS total={len(unique)} shown={next_shown} "
            f"omitted={len(unique) - next_shown} failures={failures}"
        )
        if len("\n".join([*shown_lines, line, footer])) > max_chars:
            break
        shown_lines.append(line)

    footer = (
        f"COUNTS total={len(unique)} shown={len(shown_lines)} "
        f"omitted={len(unique) - len(shown_lines)} "
        f"failures={failures}"
    )
    print("\n".join([*shown_lines, footer]))
```

The helper places the first row for each material key before secondary excerpts and shrinks primary
excerpts before omitting a key. It is only a projection: derive coverage from the full in-memory or
persisted rows, not from the visible subset. Adapt or replace it when a simpler fixed summary fits.
