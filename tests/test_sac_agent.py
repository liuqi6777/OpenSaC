from __future__ import annotations

import json
from inspect import signature
from types import SimpleNamespace
from typing import Any

import httpx

from sac_agent.client import LLMResponse, ModelClient, ModelConfig, ToolCall
from sac_agent.react import ReactAgent
from sac_agent.tool_sac import SacConfig, SacRunTool

FINAL_OUTPUT = "preamble\n<answer>literal text</answer>\nepilogue\n"


def test_sac_tool_constructor_has_no_mcp_lifecycle_policy() -> None:
    parameters = signature(SacRunTool).parameters

    assert {
        "session_create",
        "delete_on_close",
        "raise_state_loss",
        "redact_errors",
    }.isdisjoint(parameters)


def test_sac_tool_prompt_teaches_core_research_protocol() -> None:
    tool = SacRunTool()
    prompt = tool.system_prompt_addendum

    assert tool.schema["function"]["description"] == (
        "Run one Python research stage in the current OpenSAC session."
    )
    assert len(prompt) < 6_000
    normalized = " ".join(prompt.split())

    for guidance in (
        "explicit Python rule can choose the next input",
        "requires language judgment",
        "A search-only stage is valid",
        "`print` is intermediate scratch output",
        "`sdk.output.submit` is the terminal research result",
        "Stdout is not completion",
        "submitted output",
        "Pass URL/local-ID strings, never result records",
        "Public web URLs can be read directly and reused across runs",
        "inspect non-empty text",
        "read lines are 1-based, character positions are 0-based",
        "`window.next` continues losslessly",
        "there is no `sdk.workspace` API",
        "or certify citation labels",
        "Even an Explore then Verify flow can remain stateless",
        "Passing five selected sources to the next stage needs no workspace",
        "Upgrade to `sdk.state` only when",
        "candidate pool, evidence ledger, or attempted-source history",
        "do not replay blindly",
        'Branch on `status == "success"`',
        "do not parse other statuses",
    ):
        assert guidance in normalized

    assert "Core primitives:" not in prompt
    assert "For a known entity, start with" not in prompt
    assert "Print or submit" not in prompt

    examples = [part.split("```", 1)[0] for part in prompt.split("```python")[1:]]
    assert len(examples) == 3
    assert max(len(example.splitlines()) for example in examples) <= 28
    for example in examples:
        compile(example, "<sac-agent-prompt-example>", "exec")
    assert "sdk.search.many(" in examples[1]
    assert 'print("NEXT: choose sources and checks")' in examples[1]
    assert examples[2].index("sdk.content.grep(") < examples[2].index("sdk.content.read(")
    assert examples[2].index("sdk.content.read(") < examples[2].index("sdk.output.submit(")


class FakeModelClient:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_schemas: list[list[dict[str, Any]]] = []

    async def complete(self, *, messages, tools) -> LLMResponse:
        self.tool_schemas.append(tools)
        self.calls += 1
        if self.calls <= 2:
            code = f"print('stage {self.calls}')"
            return LLMResponse(
                text="",
                reasoning="",
                tool_calls=[
                    ToolCall(
                        id=f"call_{self.calls}",
                        name="sac_run",
                        arguments={"code": code},
                        raw_arguments=json.dumps({"code": code}),
                    )
                ],
                metadata={},
            )
        return LLMResponse(
            text=FINAL_OUTPUT,
            reasoning="",
            tool_calls=[],
            metadata={},
        )


async def test_model_runtime_settings_are_fixed(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MODEL_NAME", "control-model")
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_API_BASE", "http://model.test/v1")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "1")
    monkeypatch.setenv("AGENT_TEMPERATURE", "0.01")
    monkeypatch.setenv("AGENT_EXTRA_BODY", '{"ignored": true}')

    config = ModelConfig.from_env()
    assert config == ModelConfig(
        model="control-model",
        api_key="test-key",
        base_url="http://model.test/v1",
    )

    requests: list[dict[str, Any]] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(
                content="done",
                reasoning=None,
                reasoning_content=None,
                tool_calls=[],
            )
            return SimpleNamespace(
                id="response-1",
                model="control-model",
                usage=None,
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
            )

    client = ModelClient.__new__(ModelClient)
    client.config = config
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    await client.complete(messages=[], tools=[])

    assert requests == [
        {
            "model": "control-model",
            "messages": [],
            "tools": [],
            "tool_choice": "auto",
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 0.0,
            "max_tokens": 16_384,
        }
    ]


async def test_sac_runtime_settings_are_fixed(monkeypatch) -> None:
    monkeypatch.setenv("SAC_API_BASE", "http://opensac.test")
    monkeypatch.setenv("SAC_API_KEY", "test-key")
    monkeypatch.setenv("SAC_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("SAC_OUTPUT_LIMIT", "4")

    config = SacConfig.from_env()
    assert config == SacConfig(
        api_base="http://opensac.test",
        api_key="test-key",
    )

    tool = SacRunTool(config)
    try:
        assert tool._http().timeout.read == 300.0
        rendered = tool._render({"exit_code": 0, "stdout": "x" * 100})
        assert "x" * 100 in rendered
        assert "elided" not in rendered
    finally:
        await tool.aclose()


def test_sac_observation_prioritizes_external_failure_warnings() -> None:
    rendered = SacRunTool()._render(
        {
            "exit_code": 0,
            "duration_seconds": 0.1,
            "stdout": "successful passage",
            "stderr": "",
            "warnings": [
                {
                    "code": "external_result_failure",
                    "method": "content.passages",
                    "success_count": 1,
                    "failure_count": 1,
                    "failures": [
                        {
                            "source": "https://example.com/missing",
                            "code": "provider_not_found",
                            "message": "Document was not found.",
                            "retryable": False,
                        }
                    ],
                    "omitted_failure_count": 0,
                }
            ],
            "usage": {"search_calls": 0, "content_fetches": 2},
        }
    )

    assert rendered.index("warnings:") < rendered.index("stdout:")
    assert "content.passages succeeded for 1 item(s); 1 failed" in rendered
    assert "provider_not_found" in rendered
    assert "successful passage" in rendered


async def test_react_reuses_one_sac_session_and_closes_it() -> None:
    sessions_created = 0
    session_payloads: list[dict[str, Any]] = []
    exec_session_ids: list[str] = []
    deleted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal sessions_created
        if request.method == "POST" and request.url.path == "/v1/sessions":
            sessions_created += 1
            session_payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "session-1"})
        if request.method == "POST" and request.url.path.endswith("/exec"):
            exec_session_ids.append(request.url.path.split("/")[3])
            return httpx.Response(
                200,
                json={
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout": "ok",
                    "stderr": "",
                    "output": None,
                    "citations": [],
                    "artifacts": ["pool.jsonl"],
                    "usage": {"search_calls": 1, "content_fetches": 0},
                    "error": None,
                },
            )
        if request.method == "DELETE":
            deleted.append(request.url.path.split("/")[3])
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404)

    model = FakeModelClient()
    tool = SacRunTool(
        SacConfig(api_base="http://opensac.test"),
        transport=httpx.MockTransport(handle),
    )
    result = await ReactAgent(client=model, tool=tool).arun("question")

    assert result.answer == FINAL_OUTPUT
    assert result.termination == "answer"
    assert sessions_created == 1
    assert session_payloads == [{}]
    assert exec_session_ids == ["session-1", "session-1"]
    assert deleted == ["session-1"]
    assert all(len(schemas) == 1 for schemas in model.tool_schemas)
    assert model.tool_schemas[0][0]["function"]["name"] == "sac_run"


async def test_sac_rejects_empty_code_without_creating_session() -> None:
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    tool = SacRunTool(transport=httpx.MockTransport(handle))
    try:
        result = await tool.call({"code": "  "})
    finally:
        await tool.aclose()

    assert "non-empty" in result
    assert requests == 0


async def test_session_delete_failure_does_not_replace_answer() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "session-1"})
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": "ok"})
        if request.method == "DELETE":
            return httpx.Response(500, json={"detail": "cleanup failed"})
        return httpx.Response(404)

    model = FakeModelClient()
    tool = SacRunTool(transport=httpx.MockTransport(handle))
    result = await ReactAgent(client=model, tool=tool).arun("question")

    assert result.answer == FINAL_OUTPUT
    assert tool.close_error is not None
