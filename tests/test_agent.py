from __future__ import annotations

from types import SimpleNamespace

from opensac.agent.controller import AgentController
from opensac.models import Run
from opensac.sandbox import SandboxResult


class FakeCompletions:
    def __init__(self) -> None:
        self.responses = iter(
            [
                "```python\nfrom opensac_sdk import sdk\nsdk.output.submit({'evidence': []})\n```",
                '<final>{"answer":"complete","citations":[]}</final>',
            ]
        )

    async def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.responses)))],
            usage=SimpleNamespace(total_tokens=10),
        )


class FakeSandbox:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return SandboxResult(0, "", "", 0.1, output={"evidence": []})


class CitationCompletions:
    def __init__(self) -> None:
        self.responses = iter(
            [
                "```python\nfrom opensac_sdk import sdk\nsdk.output.submit({})\n```",
                '<final>{"answer":"bad","citations":[{"url":"https://invented.test"}]}</final>',
                '<final>{"answer":"grounded","citations":[{"ref":"ref_1"}]}</final>',
            ]
        )

    async def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(self.responses)))],
            usage=None,
        )


class CitationSandbox:
    async def execute(self, request):
        return SandboxResult(
            0,
            "",
            "",
            0.1,
            output={},
            citations=[{"ref": "ref_1", "url": "https://trusted.test"}],
        )


async def test_controller_executes_code_then_finishes(tmp_path) -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    sandbox = FakeSandbox()
    controller = AgentController(client, sandbox, default_model="test")
    run = Run(id="run_test", session_id="sess_test", input="research")
    result = await controller.execute(run, workspace=tmp_path, session_token="token", max_turns=3)
    assert result.status == "completed"
    assert result.output == "complete"
    assert result.usage.model_tokens == 20
    assert len(sandbox.requests) == 1
    assert sandbox.requests[0].session_id == "sess_test"


async def test_controller_rejects_unobserved_final_citations(tmp_path) -> None:
    client = SimpleNamespace(chat=SimpleNamespace(completions=CitationCompletions()))
    controller = AgentController(client, CitationSandbox(), default_model="test")
    run = Run(id="run_test", session_id="sess_test", input="research")
    result = await controller.execute(run, workspace=tmp_path, session_token="token", max_turns=3)
    assert result.status == "completed"
    assert result.output == "grounded"
    assert result.citations == [{"ref": "ref_1"}]
