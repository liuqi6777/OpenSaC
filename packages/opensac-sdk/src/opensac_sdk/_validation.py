from __future__ import annotations

import math
from typing import Any


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
    maximum: int | None = None,
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


def string_list(
    value: Any,
    name: str,
    *,
    max_items: int | None = None,
    item_max_chars: int | None = None,
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
            max_chars=item_max_chars,
        )
        for index, item in enumerate(value)
    ]


def optional_string_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    return string_list(value, name)


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value
