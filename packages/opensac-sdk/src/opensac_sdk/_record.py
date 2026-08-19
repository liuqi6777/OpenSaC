from __future__ import annotations

from typing import Any


class Record(dict[str, Any]):
    """A JSON object with both mapping and attribute reads."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            fields = ", ".join(sorted(self)) or "(no fields)"
            message = f"{name!r} is not a field of this record. It has: {fields}"
            raise AttributeError(message) from None


def wrap(value: Any) -> Any:
    if isinstance(value, Record):
        return value
    if isinstance(value, dict):
        return Record((key, wrap(item)) for key, item in value.items())
    if isinstance(value, list):
        return [wrap(item) for item in value]
    return value


def record(value: Record | dict[str, Any]) -> Record:
    return wrap(value)
