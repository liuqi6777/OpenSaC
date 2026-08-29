from __future__ import annotations

import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    """Encode a logical provider value with stable normalization and ordering."""

    def normalize(item: Any) -> Any:
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        if isinstance(item, set):
            return sorted(item, key=repr)
        return str(item)

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=normalize,
    ).encode("utf-8")
