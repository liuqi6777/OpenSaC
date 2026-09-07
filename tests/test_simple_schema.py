import json

import pytest

from opensac.errors import CapabilityError
from opensac.structured import parse_extraction, schema_validator

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": ["string", "null"], "enum": ["yes", None]},
        },
        "count": {"type": "integer"},
    },
    "required": ["items", "count"],
    "additionalProperties": False,
}


def test_nested_nullable_enum():
    data = {"items": ["yes", None], "count": 2}
    assert parse_extraction(json.dumps(data), schema_validator(SCHEMA)) == data


@pytest.mark.parametrize(
    "data",
    [
        {"items": [], "count": True},
        {"items": [], "count": 1.0},
        {"items": [], "count": "1"},
        {"items": ["no"], "count": 1},
        {"items": []},
        {"items": [], "count": 1, "extra": 0},
    ],
)
def test_output_contract(data):
    with pytest.raises(CapabilityError) as exc:
        parse_extraction(json.dumps(data), schema_validator(SCHEMA))
    assert exc.value.code == "structured_output_invalid"


@pytest.mark.parametrize(
    "schema",
    [
        {},
        {"type": None},
        {"type": ["string", "string"]},
        {"type": "array"},
        {"type": "string", "pattern": ".*"},
        {"type": "string", "anyOf": [{"type": "string"}]},
        {"type": "object", "$ref": "#"},
        {"type": "integer", "enum": [True]},
        {"type": "object", "required": ["missing"]},
        {"type": "object", "additionalProperties": {"type": "string"}},
    ],
)
def test_unsupported_or_malformed_schema(schema):
    with pytest.raises(CapabilityError) as exc:
        schema_validator(schema)
    assert exc.value.code == "invalid_schema"


def test_schema_depth_size_and_cycles():
    deep = {"type": "string"}
    for _ in range(9):
        deep = {"type": "array", "items": deep}
    cyclic = {"type": "array"}
    cyclic["items"] = cyclic
    for schema in [deep, cyclic, {"type": "string", "description": "x" * 50_000}]:
        with pytest.raises(CapabilityError) as exc:
            schema_validator(schema)
        assert exc.value.code == "invalid_schema"


@pytest.mark.parametrize(
    "text",
    [
        '{"extra": 1e400}',
        '{"extra": NaN}',
        '{"extra":' + "[" * 16 + "0" + "]" * 16 + "}",
        json.dumps({"extra": [0] * 20_000}),
    ],
)
def test_additional_fields_still_obey_output_limits(text):
    with pytest.raises(CapabilityError) as exc:
        parse_extraction(text, schema_validator({"type": "object"}))
    assert exc.value.code == "structured_output_invalid"


def test_utf8_byte_limit():
    schema = schema_validator({"type": "string"})
    assert parse_extraction('"中"', schema, max_bytes=5) == "中"
    with pytest.raises(CapabilityError) as exc:
        parse_extraction('"中"', schema, max_bytes=4)
    assert exc.value.code == "structured_output_invalid"
