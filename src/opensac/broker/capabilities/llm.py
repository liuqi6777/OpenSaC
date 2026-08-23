from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator
    from openai import AsyncOpenAI

from opensac._contracts import ExtractionRow
from opensac.broker.call_context import current_call, trace_error_message
from opensac.broker.session import BrokerSession
from opensac.broker.validation import (
    finite_number,
    integer,
    optional_integer,
    optional_string,
    string,
)
from opensac.metrics import CapacityGate
from opensac.models import ModelAttemptRecord


@dataclass(frozen=True)
class _ExtractionError:
    code: str
    message: str
    retryable: bool = False

    def wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class _ModelOutput:
    content: str | None
    tokens: int
    duration_seconds: float
    provider_failed: bool = False


class LLMCapabilities:
    """Implement bounded completion and schema-checked extraction calls."""

    def __init__(
        self,
        model_client: AsyncOpenAI | None,
        extraction_model: str,
        capacity_gate: CapacityGate,
        *,
        max_extract_items: int,
        max_instruction_bytes: int,
        max_schema_bytes: int,
        max_item_bytes: int,
        max_total_item_bytes: int,
        max_schema_depth: int,
        max_repair_attempts: int,
    ) -> None:
        self.model_client = model_client
        self.extraction_model = extraction_model
        self.capacity_gate = capacity_gate
        self.max_extract_items = max_extract_items
        self.max_extract_instruction_bytes = max_instruction_bytes
        self.max_extract_schema_bytes = max_schema_bytes
        self.max_extract_item_bytes = max_item_bytes
        self.max_extract_total_item_bytes = max_total_item_bytes
        self.max_extract_schema_depth = max_schema_depth
        self.max_extract_repair_attempts = max_repair_attempts

    async def _chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> tuple[str, int]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["max_completion_tokens"] = max_tokens
        if json_object:
            options["response_format"] = {"type": "json_object"}
        async with self.capacity_gate.slot():
            response = await self.model_client.chat.completions.create(
                model=self.extraction_model,
                messages=messages,
                **options,
            )
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return response.choices[0].message.content or "", tokens

    def _require_model(self) -> None:
        if self.model_client is None or not self.extraction_model:
            raise RuntimeError("LLM access is not configured")

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
        self._require_model()
        max_tokens = await state.policy.reserve_llm(
            1,
            max_tokens=requested_max_tokens,
        )
        answer, tokens = await self._chat(
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

    async def complete_many(self, state: BrokerSession, params: dict[str, Any]) -> list[str]:
        raw_prompts = params.get("prompts", [])
        if not isinstance(raw_prompts, list):
            raise ValueError("prompts must be a list")
        if any(not isinstance(prompt, str) for prompt in raw_prompts):
            raise ValueError("prompts must contain only strings")
        prompts = list(raw_prompts)
        system = optional_string(params.get("system"), "system")
        temperature = self._validate_temperature(params.get("temperature", 0.2))
        requested_max_tokens = self._validate_max_tokens(params.get("max_tokens"))
        concurrency = integer(
            params.get("concurrency", 4),
            "concurrency",
            minimum=1,
            maximum=12,
        )
        self._require_model()
        if not prompts:
            return []
        if any(not prompt.strip() for prompt in prompts):
            raise ValueError("prompts must not contain empty strings")
        # The whole fan-out is counted before it runs, so a batch that dies
        # partway is still reported at the size it was dispatched at rather than
        # at however far it got.
        max_tokens = await state.policy.reserve_llm(
            len(prompts),
            max_tokens=requested_max_tokens,
        )
        gate = asyncio.Semaphore(concurrency)

        async def one(prompt: str) -> tuple[str, int]:
            async with gate:
                return await self._chat(
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        results = await asyncio.gather(*(one(prompt) for prompt in prompts))
        total_tokens = sum(tokens for _, tokens in results)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        context = current_call()
        if context is not None:
            context.model_tokens += total_tokens
        return [answer for answer, _ in results]

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
    ) -> tuple[list[Any], list[str], str, str, Draft202012Validator, int, int]:
        items = params.get("items", [])
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        if len(items) > self.max_extract_items:
            raise ValueError(
                f"extract_many contains {len(items)} items, exceeding the broker maximum "
                f"of {self.max_extract_items}"
            )
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

        item_json: list[str] = []
        total_item_bytes = 0
        for index, item in enumerate(items):
            encoded = self._json_payload(item, f"item at index {index}")
            size = len(encoded.encode("utf-8"))
            if size > self.max_extract_item_bytes:
                raise ValueError(
                    f"item at index {index} is {size} bytes, exceeding the broker maximum "
                    f"of {self.max_extract_item_bytes}"
                )
            total_item_bytes += size
            item_json.append(encoded)
        if total_item_bytes > self.max_extract_total_item_bytes:
            raise ValueError(
                f"items total {total_item_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_total_item_bytes}"
            )

        repair_attempts = params.get("repair_attempts", 0)
        if isinstance(repair_attempts, bool) or not isinstance(repair_attempts, int):
            raise ValueError("repair_attempts must be 0 or 1")
        if repair_attempts not in {0, 1}:
            raise ValueError("repair_attempts must be 0 or 1")
        if repair_attempts > self.max_extract_repair_attempts:
            raise ValueError(
                f"repair_attempts exceeds the broker maximum of {self.max_extract_repair_attempts}"
            )
        concurrency = integer(
            params.get("concurrency", 4),
            "concurrency",
            minimum=1,
            maximum=12,
        )
        return (
            items,
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
            concurrency,
        )

    async def _model_output(
        self,
        prompt: str,
        *,
        max_tokens: int | None,
        gate: asyncio.Semaphore,
    ) -> _ModelOutput:
        started = time.monotonic()
        try:
            async with gate:
                content, tokens = await self._chat(
                    prompt,
                    max_tokens=max_tokens,
                    json_object=True,
                )
        except Exception:
            return _ModelOutput(
                content=None,
                tokens=0,
                duration_seconds=time.monotonic() - started,
                provider_failed=True,
            )
        return _ModelOutput(
            content=content,
            tokens=tokens,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _strict_json_object(
        content: str | None,
    ) -> tuple[dict[str, Any] | None, _ExtractionError | None]:
        if content is None or not content.strip():
            return None, _ExtractionError("empty_output", "Model returned an empty output")

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
            return None, _ExtractionError(
                "invalid_json",
                "Model returned invalid strict JSON",
            )
        if not isinstance(parsed, dict):
            return None, _ExtractionError(
                "non_object",
                "Model output must be one JSON object",
            )
        return parsed, None

    @classmethod
    def _checked_extraction(
        cls,
        content: str | None,
        validator: Draft202012Validator,
    ) -> tuple[dict[str, Any] | None, _ExtractionError | None]:
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
            return None, _ExtractionError(
                "schema_mismatch",
                f"Model output does not match schema at {location} ({first.validator})",
            )
        return data, None

    async def _record_model_tokens(
        self,
        state: BrokerSession,
        outputs: list[_ModelOutput],
    ) -> None:
        total_tokens = sum(output.tokens for output in outputs)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        context = current_call()
        if context is not None:
            context.model_tokens += total_tokens

    @staticmethod
    def _append_model_attempts(
        indexes: list[int],
        phase: str,
        outputs: list[_ModelOutput],
        errors: list[_ExtractionError | None],
    ) -> None:
        context = current_call()
        if context is None:
            return
        for index, output, error in zip(indexes, outputs, errors, strict=True):
            code = "provider_error" if output.provider_failed else error.code if error else None
            context.model_attempts.append(
                ModelAttemptRecord(
                    index=index,
                    phase=phase,
                    status="error" if code else "ok",
                    duration_seconds=output.duration_seconds,
                    model_tokens=output.tokens,
                    error_code=code,
                )
            )

    async def extract_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        (
            items,
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
            concurrency,
        ) = self._prepare_extraction(params)
        requested_max_tokens = self._validate_max_tokens(params.get("max_tokens"))
        self._require_model()
        if not items:
            return []
        max_tokens = await state.policy.reserve_llm(
            len(items),
            max_tokens=requested_max_tokens,
        )
        gate = asyncio.Semaphore(concurrency)

        def initial_prompt(index: int) -> str:
            prompt = (
                f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
                f"Input:\n{item_json[index]}\n\n"
                "Return only one JSON object."
            )
            return prompt

        initial_outputs = await asyncio.gather(
            *(
                self._model_output(initial_prompt(index), max_tokens=max_tokens, gate=gate)
                for index in range(len(items))
            )
        )
        await self._record_model_tokens(state, initial_outputs)

        checked: list[tuple[dict[str, Any] | None, _ExtractionError | None]] = []
        for output in initial_outputs:
            if output.provider_failed:
                checked.append(
                    (
                        None,
                        _ExtractionError(
                            "provider_error",
                            "Extraction provider request failed",
                            retryable=True,
                        ),
                    )
                )
            else:
                checked.append(self._checked_extraction(output.content, validator))
        self._append_model_attempts(
            list(range(len(items))),
            "initial",
            initial_outputs,
            [error for _, error in checked],
        )

        results = [
            ExtractionRow(
                index=index,
                data=data,
                failure=error.wire() if error else None,
                attempts=1,
            ).model_dump(mode="json")
            for index, (data, error) in enumerate(checked)
        ]
        repair_indexes = [
            index
            for index, (_, error) in enumerate(checked)
            if repair_attempts
            and error is not None
            and error.code in self._REPAIRABLE_EXTRACTION_ERRORS
        ]
        if not repair_indexes:
            return results

        # Reserve the complete, index-ordered repair set before dispatching any
        # second attempt. A tight budget cannot make completion order decide
        # which malformed rows get repaired.
        repair_max_tokens = await state.policy.reserve_llm(
            len(repair_indexes),
            max_tokens=requested_max_tokens,
        )

        def repair_prompt(index: int) -> str:
            assert initial_outputs[index].content is not None
            error = checked[index][1]
            assert error is not None
            return (
                f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
                f"Input:\n{item_json[index]}\n\n"
                f"Previous invalid output:\n{initial_outputs[index].content}\n\n"
                f"Validation error:\n{error.code}: {error.message}\n\n"
                "Repair the output. Return only one JSON object matching the schema."
            )

        repair_outputs = await asyncio.gather(
            *(
                self._model_output(
                    repair_prompt(index),
                    max_tokens=repair_max_tokens,
                    gate=gate,
                )
                for index in repair_indexes
            )
        )
        await self._record_model_tokens(state, repair_outputs)
        repair_checked: list[tuple[dict[str, Any] | None, _ExtractionError | None]] = []
        for output in repair_outputs:
            if output.provider_failed:
                repair_checked.append(
                    (
                        None,
                        _ExtractionError(
                            "provider_error",
                            "Extraction provider request failed",
                            retryable=True,
                        ),
                    )
                )
            else:
                repair_checked.append(self._checked_extraction(output.content, validator))
        self._append_model_attempts(
            repair_indexes,
            "repair",
            repair_outputs,
            [error for _, error in repair_checked],
        )
        for index, (data, error) in zip(repair_indexes, repair_checked, strict=True):
            results[index] = ExtractionRow(
                index=index,
                data=data,
                failure=error.wire() if error else None,
                attempts=2,
            ).model_dump(mode="json")
        return results
