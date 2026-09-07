"""Bounded, in-process validation of a small JSON Schema subset."""

import json
import math
from typing import Any

from .errors import InvalidSchemaError, StructuredOutputError

_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_COMMON = {"type", "description", "title", "enum"}


def _bound(value: Any, *, depth: int, nodes: int, chars: int) -> None:
    """Bound traversal before validation, including arbitrary additional object fields."""
    pending = [(value, 0)]
    while pending:
        item, level = pending.pop()
        nodes -= 1
        if nodes < 0 or level > depth:
            raise ValueError("JSON nesting or node limit exceeded")
        if isinstance(item, dict):
            if len(item) > nodes:
                raise ValueError("Too many fields")
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                chars -= len(key)
                pending.append((child, level + 1))
        elif isinstance(item, list):
            if len(item) > nodes:
                raise ValueError("Too many items")
            pending.extend((child, level + 1) for child in item)
        elif isinstance(item, str):
            chars -= len(item)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("Non-finite number")
        elif item is not None and type(item) not in (int, bool):
            raise ValueError("Expected JSON data")
        if chars < 0:
            raise ValueError("JSON text limit exceeded")


def _types(schema: dict[str, Any]) -> list[str]:
    value = schema.get("type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError("Expected a type name or list of type names")


def _matches(value: Any, kind: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]


def schema_validator(schema: dict[str, Any]) -> dict[str, Any]:
    """Check simple schemas once; reject every unsupported keyword explicitly."""
    try:
        _bound(schema, depth=24, nodes=2000, chars=50_000)
        if len(json.dumps(schema, allow_nan=False)) > 50_000:
            raise ValueError("Schema too large")

        def check(node: Any, depth: int = 0) -> None:
            if not isinstance(node, dict) or depth > 8:
                raise ValueError("Expected a schema object within depth 8")
            kinds = _types(node)
            if (
                not kinds
                or any(not isinstance(k, str) or k not in _TYPES for k in kinds)
                or len(set(kinds)) != len(kinds)
            ):
                raise ValueError("Expected explicit supported types")
            allowed = _COMMON | (
                {"properties", "required", "additionalProperties"} if "object" in kinds else set()
            )
            allowed |= {"items"} if "array" in kinds else set()
            if node.keys() - allowed:
                raise ValueError("Unsupported schema keyword")
            for name in ("title", "description"):
                if name in node and not isinstance(node[name], str):
                    raise ValueError("Expected text annotation")
            if "enum" in node:
                values = node["enum"]
                if not isinstance(values, list) or not 1 <= len(values) <= 100:
                    raise ValueError("Enum must have 1-100 scalar values")
                for value in values:
                    if isinstance(value, (dict, list)) or not any(
                        _matches(value, k) for k in kinds
                    ):
                        raise ValueError("Enum values must match their scalar types")
            if "object" in kinds:
                properties = node.get("properties", {})
                required = node.get("required", [])
                if not isinstance(properties, dict) or not isinstance(required, list):
                    raise ValueError("Invalid object fields")
                if any(not isinstance(k, str) or k not in properties for k in required):
                    raise ValueError("Required fields must be declared")
                if (
                    len(set(required)) != len(required)
                    or type(node.get("additionalProperties", True)) is not bool
                ):
                    raise ValueError("Invalid object rules")
                for child in properties.values():
                    check(child, depth + 1)
            if "array" in kinds:
                check(node.get("items"), depth + 1)

        check(schema)
        return schema
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidSchemaError("Invalid or unsupported simple schema.") from exc


def parse_extraction(text: str, schema: dict[str, Any], *, max_bytes: int = 2_000_000) -> Any:
    def reject(value: str) -> Any:
        raise ValueError("Non-JSON number")

    def validate(value: Any, node: dict[str, Any]) -> None:
        if not any(_matches(value, k) for k in _types(node)):
            raise ValueError("Wrong type")
        if "enum" in node and not any(
            value == item and (type(value) is bool) == (type(item) is bool) for item in node["enum"]
        ):
            raise ValueError("Value outside enum")
        if isinstance(value, dict):
            properties = node.get("properties", {})
            if any(name not in value for name in node.get("required", [])):
                raise ValueError("Missing required field")
            if not node.get("additionalProperties", True) and value.keys() - properties.keys():
                raise ValueError("Unexpected field")
            for name, child in properties.items():
                if name in value:
                    validate(value[name], child)
        elif isinstance(value, list):
            for item in value:
                validate(item, node["items"])

    try:
        if len(text) > max_bytes or len(text.encode("utf-8")) > max_bytes:
            raise ValueError("Output exceeds byte limit")
        data = json.loads(text, parse_constant=reject)
        _bound(data, depth=16, nodes=20_000, chars=max_bytes)
        validate(data, schema)
        return data
    except (ValueError, TypeError, RecursionError) as exc:
        raise StructuredOutputError("Model output violates the schema or data limits.") from exc
