from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

from opensac.broker.call_context import current_call, trace_error_message
from opensac.broker.failures import CapabilityFailure
from opensac.broker.services.llm import LLMService
from opensac.broker.session import BrokerSession
from opensac.broker.validation import (
    finite_number,
    optional_integer,
    optional_string,
    string,
)
from opensac.tracing import ModelAttemptRecord

from ..providers.execution import CapabilityProviderError


@dataclass(frozen=True)
class _ModelOutput:
    content: str | None
    tokens: int
    duration_seconds: float


class LLMCapabilities:
    """Implement bounded completion and schema-checked extraction calls."""

    def __init__(
        self,
        service: LLMService | None,
        *,
        max_instruction_bytes: int,
        max_schema_bytes: int,
        max_item_bytes: int,
        max_schema_depth: int,
        max_repair_attempts: int,
    ) -> None:
        self.service = service
        self.max_extract_instruction_bytes = max_instruction_bytes
        self.max_extract_schema_bytes = max_schema_bytes
        self.max_extract_item_bytes = max_item_bytes
        self.max_extract_schema_depth = max_schema_depth
        self.max_extract_repair_attempts = max_repair_attempts

    async def _chat(
        self,
        state: BrokerSession,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
        request_index: int = 0,
    ) -> tuple[str, int]:
        service = self._require_service()
        response = await service.complete(
            state,
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_object=json_object,
            request_index=request_index,
        )
        return response.content, response.tokens

    def _require_service(self) -> LLMService:
        if self.service is None:
            raise RuntimeError("LLM access is not configured")
        return self.service

    @staticmethod
    def _validate_temperature(value: Any) -> float:
        return finite_number(value, "temperature", minimum=0.0, maximum=2.0)

    @staticmethod
    def _validate_max_tokens(value: Any) -> int | None:
        return optional_integer(
            value,
            "max_tokens",
            minimum=1,
            maximum=32_000,
        )

    async def complete(self, state: BrokerSession, params: dict[str, Any]) -> str:
        prompt = string(params.get("prompt", ""), "prompt")
        system = optional_string(params.get("system"), "system")
        temperature = self._validate_temperature(params.get("temperature", 0.2))
        requested_max_tokens = self._validate_max_tokens(params.get("max_tokens"))
        self._require_service()
        max_tokens = await state.policy.reserve_llm(
            1,
            max_tokens=requested_max_tokens,
        )
        answer, tokens = await self._chat(
            state,
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        await state.policy.record_pipeline_model_tokens(tokens)
        context = current_call()
        if context is not None:
            context.model_tokens += tokens
        return answer

    _SCHEMA_KEYWORDS = frozenset(
        {
            "$schema",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "description",
        }
    )
    _SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
    _REPAIRABLE_EXTRACTION_ERRORS = frozenset(
        {"empty_output", "invalid_json", "non_object", "schema_mismatch"}
    )

    @staticmethod
    def _json_payload(value: Any, label: str) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise ValueError(f"{label} must contain only JSON-serializable values") from None

    def _validate_schema_subset(self, schema: Any) -> Draft202012Validator:
        from jsonschema import Draft202012Validator

        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON object")
        if schema.get("type") != "object":
            raise ValueError("schema root must declare type 'object'")

        def visit(node: Any, *, depth: int, path: str) -> None:
            if not isinstance(node, dict):
                raise ValueError(f"schema node at {path} must be an object")
            if depth > self.max_extract_schema_depth:
                raise ValueError(
                    f"schema nesting exceeds maximum depth {self.max_extract_schema_depth}"
                )
            unknown = sorted(set(node) - self._SCHEMA_KEYWORDS)
            if unknown:
                raise ValueError(f"schema keyword '{unknown[0]}' at {path} is not supported")
            declared_type = node.get("type")
            base_type: str | None = None
            if declared_type is not None:
                if isinstance(declared_type, str):
                    if declared_type not in self._SCHEMA_TYPES:
                        raise ValueError(
                            f"schema type '{declared_type}' at {path} is not supported"
                        )
                    base_type = declared_type
                elif isinstance(declared_type, list):
                    if (
                        len(declared_type) != 2
                        or any(
                            not isinstance(item, str) or item not in self._SCHEMA_TYPES
                            for item in declared_type
                        )
                        or len(set(declared_type)) != 2
                        or "null" not in declared_type
                    ):
                        raise ValueError(
                            f"schema type list at {path} must contain one type plus null"
                        )
                    base_type = next(item for item in declared_type if item != "null")
                else:
                    raise ValueError(f"schema type at {path} must be a string or list")

            if "$schema" in node:
                dialect = node["$schema"]
                if dialect not in {
                    "https://json-schema.org/draft/2020-12/schema",
                    "https://json-schema.org/draft/2020-12/schema#",
                }:
                    raise ValueError("only JSON Schema Draft 2020-12 is supported")
            if "description" in node and not isinstance(node["description"], str):
                raise ValueError(f"schema description at {path} must be a string")
            if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
                raise ValueError(f"schema enum at {path} must be a non-empty list")

            object_keys = {"properties", "required", "additionalProperties"} & set(node)
            if object_keys and base_type not in {None, "object"}:
                raise ValueError(f"object keywords at {path} require type 'object'")
            properties = node.get("properties")
            if properties is not None:
                if not isinstance(properties, dict):
                    raise ValueError(f"schema properties at {path} must be an object")
                for name, child in properties.items():
                    if not isinstance(name, str):
                        raise ValueError(f"schema property names at {path} must be strings")
                    visit(child, depth=depth + 1, path=f"{path}.properties.{name}")
            if "required" in node:
                required = node["required"]
                if not isinstance(required, list) or any(
                    not isinstance(item, str) for item in required
                ):
                    raise ValueError(f"schema required at {path} must be a list of strings")
            if "additionalProperties" in node and not isinstance(
                node["additionalProperties"], bool
            ):
                raise ValueError(f"schema additionalProperties at {path} must be a boolean")

            if "items" in node:
                if base_type not in {None, "array"}:
                    raise ValueError(f"schema items at {path} requires type 'array'")
                visit(node["items"], depth=depth + 1, path=f"{path}.items")

        visit(schema, depth=1, path="$")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise ValueError(f"invalid JSON Schema: {trace_error_message(exc)}") from None
        return Draft202012Validator(schema)

    def _prepare_extraction(
        self,
        params: dict[str, Any],
    ) -> tuple[str, str, str, Draft202012Validator, int]:
        if "item" not in params:
            raise ValueError("extract must provide item")
        instruction = string(
            params.get("instruction", ""),
            "instruction",
            nonempty=False,
        )
        instruction_bytes = len(instruction.encode("utf-8"))
        if instruction_bytes > self.max_extract_instruction_bytes:
            raise ValueError(
                f"instruction is {instruction_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_instruction_bytes}"
            )

        schema = params.get("schema", {})
        schema_json = self._json_payload(schema, "schema")
        schema_bytes = len(schema_json.encode("utf-8"))
        if schema_bytes > self.max_extract_schema_bytes:
            raise ValueError(
                f"schema is {schema_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_schema_bytes}"
            )
        validator = self._validate_schema_subset(schema)

        item_json = self._json_payload(params["item"], "item")
        item_bytes = len(item_json.encode("utf-8"))
        if item_bytes > self.max_extract_item_bytes:
            raise ValueError(
                f"item is {item_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_item_bytes}"
            )

        repair_attempts = params.get("repair_attempts", 0)
        if isinstance(repair_attempts, bool) or not isinstance(repair_attempts, int):
            raise ValueError("repair_attempts must be a non-negative integer")
        if repair_attempts < 0:
            raise ValueError("repair_attempts must be a non-negative integer")
        if repair_attempts > self.max_extract_repair_attempts:
            raise ValueError(
                f"repair_attempts exceeds the broker maximum of {self.max_extract_repair_attempts}"
            )
        return (
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
        )

    async def _model_output(
        self,
        state: BrokerSession,
        prompt: str,
        *,
        max_tokens: int | None,
    ) -> _ModelOutput:
        started = time.monotonic()
        content, tokens = await self._chat(
            state,
            prompt,
            max_tokens=max_tokens,
            json_object=True,
            request_index=0,
        )
        return _ModelOutput(
            content=content,
            tokens=tokens,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _strict_json_object(
        content: str | None,
    ) -> tuple[dict[str, Any] | None, CapabilityFailure | None]:
        if content is None or not content.strip():
            return None, CapabilityFailure(
                code="empty_output",
                message="Model returned an empty output",
                retryable=False,
            )

        def reject_constant(_: str) -> Any:
            raise ValueError("non-finite number")

        def finite_float(value: str) -> float:
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("non-finite number")
            return parsed

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate object key")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                content,
                parse_constant=reject_constant,
                parse_float=finite_float,
                object_pairs_hook=unique_object,
            )
        except (json.JSONDecodeError, ValueError):
            return None, CapabilityFailure(
                code="invalid_json",
                message="Model returned invalid strict JSON",
                retryable=False,
            )
        if not isinstance(parsed, dict):
            return None, CapabilityFailure(
                code="non_object",
                message="Model output must be one JSON object",
                retryable=False,
            )
        return parsed, None

    @classmethod
    def _checked_extraction(
        cls,
        content: str | None,
        validator: Draft202012Validator,
    ) -> tuple[dict[str, Any] | None, CapabilityFailure | None]:
        data, error = cls._strict_json_object(content)
        if error is not None:
            return None, error
        assert data is not None
        failures = sorted(validator.iter_errors(data), key=lambda item: list(item.path))
        if failures:
            first = failures[0]
            location = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.path
            )
            return None, CapabilityFailure(
                code="schema_mismatch",
                message=f"Model output does not match schema at {location} ({first.validator})",
                retryable=False,
            )
        return data, None

    async def _record_model_tokens(
        self,
        state: BrokerSession,
        output: _ModelOutput,
    ) -> None:
        await state.policy.record_pipeline_model_tokens(output.tokens)
        context = current_call()
        if context is not None:
            context.model_tokens += output.tokens

    @staticmethod
    def _append_model_attempt(
        phase: str,
        output: _ModelOutput,
        error: CapabilityFailure | None,
    ) -> None:
        context = current_call()
        if context is None:
            return
        context.model_attempts.append(
            ModelAttemptRecord(
                index=0,
                phase=phase,
                status="error" if error else "ok",
                duration_seconds=output.duration_seconds,
                model_tokens=output.tokens,
                error_code=error.code if error else None,
            )
        )

    async def extract(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        (
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
        ) = self._prepare_extraction(params)
        requested_max_tokens = self._validate_max_tokens(params.get("max_tokens"))
        self._require_service()
        max_tokens = await state.policy.reserve_llm(
            1,
            max_tokens=requested_max_tokens,
        )
        initial_prompt = (
            f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
            f"Input:\n{item_json}\n\n"
            "Return only one JSON object."
        )
        initial_output = await self._model_output(
            state,
            initial_prompt,
            max_tokens=max_tokens,
        )
        await self._record_model_tokens(state, initial_output)
        data, error = self._checked_extraction(initial_output.content, validator)
        self._append_model_attempt("initial", initial_output, error)
        if error is None:
            if data is None:
                raise RuntimeError("Successful extraction has no data.")
            return data
        attempts = 1
        previous_output = initial_output
        while attempts <= repair_attempts and error.code in self._REPAIRABLE_EXTRACTION_ERRORS:
            repair_max_tokens = await state.policy.reserve_llm(
                1,
                max_tokens=requested_max_tokens,
            )
            repair_prompt = (
                f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
                f"Input:\n{item_json}\n\n"
                f"Previous invalid output:\n{previous_output.content}\n\n"
                f"Validation error:\n{error.code}: {error.message}\n\n"
                "Repair the output. Return only one JSON object matching the schema."
            )
            repair_output = await self._model_output(
                state,
                repair_prompt,
                max_tokens=repair_max_tokens,
            )
            attempts += 1
            await self._record_model_tokens(state, repair_output)
            data, error = self._checked_extraction(repair_output.content, validator)
            self._append_model_attempt("repair", repair_output, error)
            if error is None:
                if data is None:
                    raise RuntimeError("Successful extraction has no data.")
                return data
            previous_output = repair_output

        raise CapabilityProviderError.from_failure(
            error.model_dump(mode="json"),
            attempts=attempts,
        )
