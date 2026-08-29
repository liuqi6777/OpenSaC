from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator

    from opensac.broker.capabilities.catalog import CapabilityBuildContext

from opensac.backends.llm import LLMBackend, LLMResponse
from opensac.broker._utils import (
    finite_number,
    optional_integer,
    optional_string,
    string,
)
from opensac.broker.call_context import current_call, trace_error_message
from opensac.broker.failures import CapabilityFailure
from opensac.broker.registry import BaseCapabilities, CapabilityRequest, capability_method
from opensac.broker.session import BrokerSession
from opensac.tracing import ModelAttemptRecord

from ..providers.execution import BackendBinding, CapabilityProviderError, ProviderExecutor


class LLMLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_completion_tokens: int = Field(default=32_000, ge=1)
    extract_max_instruction_bytes: int = Field(default=16_384, ge=1)
    extract_max_schema_bytes: int = Field(default=65_536, ge=1)
    extract_max_item_bytes: int = Field(default=65_536, ge=1)
    extract_max_schema_depth: int = Field(default=8, ge=1)
    extract_max_repair_attempts: int = Field(default=1, ge=0)


class LLMCompleteRequest(CapabilityRequest):
    prompt: str = ""
    system: str | None = None
    temperature: int | float = 0.2
    max_tokens: int | None = None


class LLMExtractRequest(CapabilityRequest):
    item: Any
    instruction: str = ""
    schema_: Any = Field(default_factory=dict, alias="schema")
    max_tokens: int | None = None
    repair_attempts: int = 0


@dataclass(frozen=True)
class _ModelOutput:
    content: str | None
    tokens: int
    duration_seconds: float


class LLMCapabilities(BaseCapabilities):
    """Implement bounded completion and schema-checked extraction calls."""

    name = "llm"

    def __init__(
        self,
        providers: ProviderExecutor,
        binding: BackendBinding[LLMBackend] | None,
        *,
        limits: LLMLimits,
        default_concurrency: int,
    ) -> None:
        self.providers = providers
        self.binding = binding
        self.limits = limits
        self.default_concurrency = default_concurrency
        self.max_extract_instruction_bytes = limits.extract_max_instruction_bytes
        self.max_extract_schema_bytes = limits.extract_max_schema_bytes
        self.max_extract_item_bytes = limits.extract_max_item_bytes
        self.max_extract_schema_depth = limits.extract_max_schema_depth
        self.max_extract_repair_attempts = limits.extract_max_repair_attempts

    @classmethod
    def from_context(cls, context: CapabilityBuildContext) -> Self:
        return cls(
            context.providers,
            context.llm_binding,
            limits=context.config.llm,
            default_concurrency=context.default_provider_concurrency,
        )

    @property
    def available(self) -> bool:
        return self.binding is not None

    def manifest(self, *, backend_name: str) -> dict[str, Any]:
        del backend_name
        return {
            "available": self.binding is not None,
            "limits": {
                "max_concurrency": (
                    self.binding.runtime.policy.concurrency
                    if self.binding is not None
                    else self.default_concurrency
                ),
                "max_completion_tokens": self.limits.max_completion_tokens,
                "extract_max_instruction_bytes": self.limits.extract_max_instruction_bytes,
                "extract_max_schema_bytes": self.limits.extract_max_schema_bytes,
                "extract_max_item_bytes": self.limits.extract_max_item_bytes,
                "extract_max_schema_depth": self.limits.extract_max_schema_depth,
                "extract_max_repair_attempts": self.limits.extract_max_repair_attempts,
            },
        }

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
        binding = self._require_binding()

        async def complete(backend: LLMBackend) -> LLMResponse:
            return LLMResponse.model_validate(
                await backend.complete(
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_object=json_object,
                )
            )

        preflight = getattr(binding.backend, "preflight", None)
        response = await self.providers.execute(
            state,
            binding,
            request_indexes=[request_index],
            request_value={
                "model": binding.backend.name,
                "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "system": (
                    hashlib.sha256(system.encode("utf-8")).hexdigest()
                    if system is not None
                    else None
                ),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_object": json_object,
            },
            request=complete,
            preflight=preflight if callable(preflight) else None,
        )
        return response.content, response.tokens

    def _require_binding(self) -> BackendBinding[LLMBackend]:
        if self.binding is None:
            raise RuntimeError("LLM access is not configured")
        return self.binding

    @staticmethod
    def _validate_temperature(value: Any) -> float:
        return finite_number(value, "temperature", minimum=0.0, maximum=2.0)

    def _validate_max_tokens(self, value: Any) -> int | None:
        return optional_integer(
            value,
            "max_tokens",
            minimum=1,
            maximum=self.limits.max_completion_tokens,
        )

    @capability_method("llm.complete", LLMCompleteRequest)
    async def complete(self, state: BrokerSession, request: LLMCompleteRequest) -> str:
        prompt = string(request.prompt, "prompt")
        system = optional_string(request.system, "system")
        temperature = self._validate_temperature(request.temperature)
        requested_max_tokens = self._validate_max_tokens(request.max_tokens)
        self._require_binding()
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
        request: LLMExtractRequest,
    ) -> tuple[str, str, str, Draft202012Validator, int]:
        instruction = string(
            request.instruction,
            "instruction",
            nonempty=False,
        )
        instruction_bytes = len(instruction.encode("utf-8"))
        if instruction_bytes > self.max_extract_instruction_bytes:
            raise ValueError(
                f"instruction is {instruction_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_instruction_bytes}"
            )

        schema = request.schema_
        schema_json = self._json_payload(schema, "schema")
        schema_bytes = len(schema_json.encode("utf-8"))
        if schema_bytes > self.max_extract_schema_bytes:
            raise ValueError(
                f"schema is {schema_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_schema_bytes}"
            )
        validator = self._validate_schema_subset(schema)

        item_json = self._json_payload(request.item, "item")
        item_bytes = len(item_json.encode("utf-8"))
        if item_bytes > self.max_extract_item_bytes:
            raise ValueError(
                f"item is {item_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_item_bytes}"
            )

        repair_attempts = request.repair_attempts
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

    @capability_method("llm.extract", LLMExtractRequest)
    async def extract(
        self,
        state: BrokerSession,
        request: LLMExtractRequest,
    ) -> dict[str, Any]:
        (
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
        ) = self._prepare_extraction(request)
        requested_max_tokens = self._validate_max_tokens(request.max_tokens)
        self._require_binding()
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
