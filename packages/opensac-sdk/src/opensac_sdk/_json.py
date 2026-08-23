from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def strict_json_dumps(
    value: Any,
    *,
    field: str,
    indent: int | None = None,
) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(f"{field} must contain only strict JSON values: {exc}") from exc


def strict_jsonl_dumps(rows: list[Any], *, field: str = "rows") -> str:
    if not isinstance(rows, list):
        raise ValueError(f"{field} must be a list")
    return "".join(
        strict_json_dumps(row, field=f"{field}[{index}]") + "\n" for index, row in enumerate(rows)
    )


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
